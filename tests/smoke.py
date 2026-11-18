# -*- coding: utf-8 -*-
"""全流程冒烟测试（离线，伪造证书页，不依赖外部网络与 rapidocr）。

运行：在项目根目录执行  python tests/smoke.py
依赖：pip install fastapi uvicorn jinja2 python-multipart httpx pillow zxing-cpp openai openpyxl
"""
import io
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["DATA_DIR"] = os.path.join(tempfile.gettempdir(), "qianbi-smoke-test")
os.environ["FETCH_INTERVAL_SECONDS"] = "0.5"   # 测试加速：抓取限频缩短
shutil.rmtree(os.environ["DATA_DIR"], ignore_errors=True)

from PIL import Image  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from openpyxl import load_workbook as _load_wb  # noqa: E402
from app.main import app  # noqa: E402

ok_count = 0


def check(name, cond, detail=""):
    global ok_count
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name} {detail}")
    if cond:
        ok_count += 1


with TestClient(app) as c:
    # ---------- 基础与健康 ----------
    h = c.get("/api/health").json()
    check("health", h["status"] == "ok", str(h))
    check("未登录拦截", c.get("/api/dict/bank").status_code == 401)
    check("错误口令拒绝", c.post("/api/login",
          data={"username": "admin", "password": "wrong"}).status_code == 401)
    check("默认管理员登录", c.post("/api/login",
          data={"username": "admin", "password": "changeme"}).json().get("ok") is True)
    me = c.get("/api/me").json()
    check("当前用户", me.get("role") == "admin", str(me))

    # ---------- 字典 ----------
    banks = c.get("/api/dict/bank").json()
    check("字典-机构", any(b["name"] == "中央银行" for b in banks), f"共{len(banks)}家")
    check("字典-别名", any("Central Bank of China" in (b["aliases"] or "") for b in banks))

    # ---------- 手动建档 → 确认 → 检索 ----------
    iid = c.post("/api/items", json={}).json()["item_id"]
    check("手动建档", iid > 0, f"item_id={iid}")
    d = c.post(f"/api/items/{iid}", json={"fields": {
        "category": "纸币", "bank": "中央银行", "note_type": "金圆券",
        "denomination": "壹佰圆", "year_text": "民国三十六年"}, "confirm": True}).json()
    check("确认入库", d["ok"] and d["item_code"], str(d))
    r = c.get("/api/items", params={"q": "中央银行"}).json()
    check("检索命中", r["total"] == 1 and r["items"][0]["year_sort"] == 1947,
          f"year_sort={r['items'][0]['year_sort'] if r['items'] else None}, "
          f"code={r['items'][0]['item_code'] if r['items'] else None}")

    # ---------- 全文检索：FTS 与 LIKE 降级 ----------
    import app.models as models
    check("FTS 可用", models.FTS_OK)
    r = c.get("/api/items", params={"q": "央银行"}).json()      # 3字，走 FTS
    check("FTS 中文检索", r["total"] == 1, f"total={r['total']}")
    r = c.get("/api/items", params={"q": "银行"}).json()        # 2字，降级 LIKE
    check("LIKE 降级检索", r["total"] == 1, f"total={r['total']}")

    # ---------- 分页 ----------
    r = c.get("/api/items", params={"limit": 1, "offset": 0}).json()
    check("分页-limit", len(r["items"]) <= 1 and r["total"] >= 1, f"total={r['total']}")
    r = c.get("/api/items", params={"limit": 1, "offset": 999}).json()
    check("分页-offset越界", r["items"] == [] and r["total"] >= 1)

    # ---------- 规则解析（模拟 PMG 风格英文标签） ----------
    from app.adapters import rules
    text = ("Certification Number 1234567  Central Bank of China $100 Gold Yuan "
            "1947  Printed by American Bank Note Company  Grading MS 63 EPQ  "
            "民国三十六年 壹佰圆")
    f = rules.parse_cert_page(text, "https://www.pmgnotes.com/PMG/cert/1234567")
    check("解析-编号", f.get("cert_no") == "1234567", str(f.get("cert_no")))
    check("解析-机构映射(最长命中)", f.get("bank") == "中央银行", str(f.get("bank")))
    check("解析-券种", f.get("note_type") == "金圆券", str(f.get("note_type")))
    check("解析-印刷厂", f.get("printer") == "美国钞票公司", str(f.get("printer")))
    check("解析-系列推断", f.get("series") == "民国纸币", str(f.get("series")))
    check("解析-分数", f.get("grade_raw") == "MS 63 EPQ" and f.get("grade_scale") == "70制",
          str(f.get("grade_raw")))
    check("解析-纪年", f.get("year_sort") == 1947, str(f.get("year_sort")))
    check("解析-名称合成", bool(f.get("name")), str(f.get("name")))

    # 国标风格（真实证书页格式实测样本）
    f4 = rules.parse_cert_page(
        "藏品编号 DZB2607050045 藏品名称 中央银行 华德路版 藏品年代 民国25年 "
        "藏品面值 拾圆 藏品冠号 650817 藏品分数 60EPQ",
        "http://api.cfcr.org.cn/x/DZB2607050045")
    check("解析-国标字段", f4.get("cert_no") == "DZB2607050045"
          and f4.get("bank") == "中央银行" and f4.get("year_sort") == 1936
          and f4.get("grade_raw") == "60EPQ" and f4.get("serial_no") == "650817",
          str({k: f4.get(k) for k in ("cert_no", "bank", "year_sort", "grade_raw", "serial_no")}))

    f2 = rules.parse_label_ocr("北海银行 边币伍拾圆 民国三十五年 美品")
    check("解析-边区机构", f2.get("bank") == "北海银行", str(f2.get("bank")))
    check("解析-边区系列", f2.get("series") == "边区·解放区", str(f2.get("series")))

    f3 = rules.parse_label_ocr("光绪十八年 山西平遥 地契 官契 美品")
    check("解析-地契类型", f3.get("doc_type") == "地契", str(f3.get("doc_type")))
    check("解析-年号纪年", f3.get("year_sort") == 1892, str(f3.get("year_sort")))

    from app.services import years
    got = [years.to_gregorian(x) for x in
           ("民国十九年", "光绪十八年", "康熙十八年", "乾隆五十年", "宣统二年", "1949年")]
    check("纪年换算", got == [1930, 1892, 1679, 1785, 1910, 1949], str(got))

    # ---------- 二维码 ----------
    qr_png = os.path.join(os.environ["DATA_DIR"], "qr_test.png")
    try:
        import qrcode
        qrcode.make("https://www.pmgnotes.com/PMG/cert/7654321").save(qr_png)
        from app.services import qr as qrsvc
        t = qrsvc.decode_qr(qr_png)
        check("二维码解码", t is not None and "7654321" in t, str(t))
    except Exception as e:
        print("[SKIP] 二维码生成解码：", e)
        qr_png = None

    # ---------- 录入流水线（伪造证书页） ----------
    import app.ingest as ing
    _real_qr = ing.qr.decode_qr
    _real_fetch = ing.webfetch.fetch_page
    _real_ocr = ing.ocr.read_text
    fake_url = "https://fake-cert.example.com/cert/777666555"
    ing.qr.decode_qr = lambda p: fake_url
    ing.webfetch.fetch_page = lambda u: {
        "url_final": u,
        "html": "<html><body>Central Bank of China Gold Yuan 1947 "
                "American Bank Note Company MS 63 EPQ 民国三十六年</body></html>"}
    ing.ocr.read_text = lambda p: None

    fake_photo = os.path.join(os.environ["DATA_DIR"], "fake_label.jpg")
    Image.new("RGB", (400, 300), "white").save(fake_photo)
    data = open(fake_photo, "rb").read()

    res = ing.ingest_label(data)
    check("流水线-证书页建草稿",
          res["ok"] and res["parsed_from"] == "qr_cert"
          and res["fields"].get("bank") == "中央银行",
          str({k: res.get(k) for k in ("ok", "item_id", "parsed_from")}))
    res2 = ing.ingest_label(data)
    check("流水线-编号判重拦截",
          res2.get("dupe") is True and res2["existing"]["id"] == res["item_id"],
          str(res2.get("existing", {}).get("id")))

    ing.webfetch.fetch_page = _real_fetch   # 还原：别污染后面的 SSRF 安全测试

    ing.qr.decode_qr = lambda p: None
    plain = os.path.join(os.environ["DATA_DIR"], "plain.jpg")
    Image.new("RGB", (400, 300), "white").save(plain)
    res3 = ing.ingest_label(open(plain, "rb").read())
    check("流水线-未识别仍建草稿",
          res3["ok"] and res3["parsed_from"] == "none", str(res3.get("item_id")))

    # ---------- 照片 ----------
    iid4 = res3["item_id"]
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), "red").save(buf, "JPEG")
    r = c.post(f"/api/items/{iid4}/photos",
               files={"photo": ("front.jpg", buf.getvalue(), "image/jpeg")},
               data={"shot_type": "front"}).json()
    check("照片上传", r.get("ok") is True, str(r)[:100])
    r = c.post(f"/api/items/{iid}/photos",
               files={"photo": ("front2.jpg", buf.getvalue(), "image/jpeg")},
               data={"shot_type": "front"}).json()
    check("aHash 查重提示(跨藏品)", r.get("ok") and len(r.get("similar", [])) >= 1,
          str(r.get("similar"))[:100])

    # ---------- 管理功能 ----------
    r = c.get("/api/pending").json()
    check("待审核队列", r["count"] >= 2, f"count={r['count']}")
    r = c.get(f"/api/items/{iid}").json()
    check("一物一档详情", r["item_code"] and isinstance(r["photos"], list))

    check("字典-新增", c.post("/api/dict", json={
        "type": "bank", "name": "测试银行", "aliases": "Test Bank", "sort": 90}).json()["ok"])
    entries = [e for e in c.get("/api/dict-all").json() if e["name"] == "测试银行"]
    check("字典-查询全部", len(entries) == 1)
    eid = entries[0]["id"]
    check("字典-修改", c.put(f"/api/dict/{eid}", json={
        "type": "bank", "name": "测试银行二", "sort": 91}).json()["ok"])
    check("字典-删除", c.delete(f"/api/dict/{eid}").json()["ok"])

    np = c.get("/api/needs-photos").json()
    check("补拍队列", np["count"] >= 1, f"count={np['count']}")

    s = c.get("/api/stats").json()
    check("统计", s["total"] >= 3 and "by_category" in s, f"total={s['total']}")

    r = c.get("/api/export/xlsx")
    check("Excel导出(管理员)", r.status_code == 200 and len(r.content) > 500
          and "spreadsheetml" in r.headers.get("content-type", ""),
          f"{len(r.content)}字节")

    # ---------- 多用户与权限 ----------
    check("创建录入员", c.post("/api/users", json={
        "username": "xiaowang", "password": "pw123456", "role": "staff"}).json()["ok"])
    check("创建只读用户", c.post("/api/users", json={
        "username": "guest", "password": "guest123", "role": "viewer"}).json()["ok"])

    # 管理员给藏品估价
    c.post(f"/api/items/{iid}", json={"fields": {"purchase_price": 123}})

    staff = TestClient(app)
    check("录入员登录", staff.post("/api/login",
          data={"username": "xiaowang", "password": "pw123456"}).json().get("ok") is True)
    d = staff.get(f"/api/items/{iid}").json()
    check("录入员看不到价格", "purchase_price" not in d and "est_value" not in d,
          f"keys有purchase_price={'purchase_price' in d}")
    check("录入员导出被拒", staff.get("/api/export/xlsx").status_code == 403)
    check("录入员用户列表被拒", staff.get("/api/users").status_code == 403)
    check("录入员字典写被拒", staff.post("/api/dict", json={
          "type": "bank", "name": "越权银行"}).status_code == 403)
    check("录入员删档案被拒", staff.delete(f"/api/items/{iid}").status_code == 403)
    check("录入员删照片允许",
          staff.delete(f"/api/photos/{staff.get(f'/api/items/{iid4}').json()['photos'][0]['id']}").status_code == 200)
    # 录入员改价被服务端丢弃
    staff.post(f"/api/items/{iid}", json={"fields": {"purchase_price": 999}})
    d = c.get(f"/api/items/{iid}").json()
    check("录入员改价无效", d.get("purchase_price") == 123, str(d.get("purchase_price")))

    viewer = TestClient(app)
    check("只读用户登录", viewer.post("/api/login",
          data={"username": "guest", "password": "guest123"}).json().get("ok") is True)
    check("只读用户可检索", viewer.get("/api/items").json().get("total", 0) >= 3)
    check("只读用户估值归零", viewer.get("/api/stats").json().get("est_total") == 0)
    check("只读用户录入被拒", viewer.post("/api/items", json={}).status_code == 403)

    # ---------- 登录锁定（函数级，不污染 HTTP 层 IP） ----------
    from app.auth import login_user
    for _ in range(5):
        login_user("ip-lock", "admin", "wrongpw")
    check("连续失败锁定", login_user("ip-lock", "admin", "changeme") is None)
    check("其他IP不受影响", login_user("ip-ok", "admin", "changeme") is not None)

    # ---------- 删除与页面 ----------
    tmp_id = c.post("/api/items", json={}).json()["item_id"]
    check("藏品删除(管理员)", c.delete(f"/api/items/{tmp_id}").json()["ok"])

    check("一物一档编辑页", c.get(f"/items/{iid}").status_code == 200)
    check("打印档案卡页", c.get(f"/items/{iid}/print").status_code == 200)
    check("字典管理页", c.get("/dict").status_code == 200)
    check("用户管理页", c.get("/users").status_code == 200)
    check("采集页", c.get("/capture").status_code == 200)
    check("登录页", c.get("/login").status_code == 200)

    # ---------- 库位 / 盘点 / 导入 / 备份 ----------
    r = c.post("/api/locations", json={"name": "一号展柜"}).json()
    check("库位-建展柜", r["ok"] and r["id"] > 0)
    root_id = r["id"]
    child_id = c.post("/api/locations",
                      json={"name": "第1层", "parent_id": root_id}).json()["id"]
    check("库位-建子层", child_id > 0)
    check("库位-改名", c.put(f"/api/locations/{root_id}", json={
        "name": "二号展柜", "parent_id": None}).json()["ok"])
    check("库位-列表", len(c.get("/api/locations").json()) == 2)

    c.post(f"/api/items/{iid}", json={"location_id": child_id})
    d = c.get(f"/api/items/{iid}").json()
    check("藏品归位", d["location_id"] == child_id)
    r = c.get("/api/items", params={"location_id": child_id}).json()
    check("按库位筛选", r["total"] == 1)
    r = c.get("/api/items", params={"location_id": -1}).json()
    check("未入库位筛选", all(it["location_id"] is None for it in r["items"]))
    check("库位-有藏品禁删", c.delete(f"/api/locations/{child_id}").status_code == 400)
    check("库位-有子级禁删", c.delete(f"/api/locations/{root_id}").status_code == 400)

    code = d["item_code"]
    r = c.get(f"/api/items/by-code/{code}").json()
    check("扫码-内部编号", r["id"] == iid and r["location_name"], f"code={code}")
    r = c.get("/api/items/by-code/777666555").json()
    check("扫码-评级编号", r is not None and r["id"] == res["item_id"])
    check("扫码-不存在", c.get("/api/items/by-code/NOPE123").status_code == 404)

    d = c.get(f"/api/items/{iid}").json()
    kinds = [e["kind"] for e in d["events"]]
    check("操作日志", "入库" in kinds and "位置变更" in kinds, str(kinds[:4]))

    from openpyxl import Workbook as _WB
    wb2 = _WB(); ws2 = wb2.active
    ws2.append(["内部编号", "名称", "品类", "系列", "发行机构", "券种/类型", "印刷厂",
                "文书类型", "朝代", "年号/品种", "面值", "版别", "材质", "纪年", "公历年",
                "地域", "评级公司", "评级编号", "分数", "分数制", "冠字号", "状态", "来源",
                "采购价", "采购日期", "估值", "估值日期", "备注", "录入时间"])
    ws2.append([None, "导入测试 光绪通宝", "铜钱", None, None, None, None, None, "清",
                "光绪通宝", "当十", None, "铜", "光绪三十一年", 1905, None, "公博",
                "IMP2026001", "美品78", "85制", None, "在库", "购入", 88, None, 120,
                None, "导入测试", None])
    b2 = io.BytesIO()
    wb2.save(b2)
    xlsx_bytes = b2.getvalue()
    xlsx_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    r = c.post("/api/import/xlsx", files={"file": ("t.xlsx", xlsx_bytes, xlsx_type)}).json()
    check("Excel导入-新增", r["imported"] == 1 and r["skipped"] == 0, str(r))
    r = c.get("/api/items", params={"q": "IMP2026001"}).json()
    check("Excel导入-按编号检索", r["total"] == 1 and r["items"][0]["year_sort"] == 1905,
          f"total={r['total']}")
    r = c.post("/api/import/xlsx", files={"file": ("t.xlsx", xlsx_bytes, xlsx_type)}).json()
    check("Excel导入-重复跳过", r["skipped"] == 1 and r["imported"] == 0, str(r))

    r = c.get("/api/backup/db")
    check("数据库备份", r.status_code == 200 and len(r.content) > 5000,
          f"{len(r.content)}字节")

    check("库位页", c.get("/locations").status_code == 200)
    check("盘点页", c.get("/inventory").status_code == 200)
    check("录入员库位页被拒", staff.get("/locations", follow_redirects=False).status_code == 303)
    check("录入员盘点页", staff.get("/inventory").status_code == 200)
    check("只读用户盘点被拒", viewer.get("/inventory", follow_redirects=False).status_code == 303)

    # ---------- 批量识别队列（真实后台线程） ----------
    import time as _time
    ing.qr.decode_qr = lambda p: "https://fake-cert.example.com/cert/888999000"
    ing.webfetch.fetch_page = lambda u: {
        "url_final": u,
        "html": "<html><body>Central Bank of China Gold Yuan 1947 MS 63 EPQ</body></html>"}
    bp1 = os.path.join(os.environ["DATA_DIR"], "b1.jpg")
    bp2 = os.path.join(os.environ["DATA_DIR"], "b2.jpg")
    Image.new("RGB", (300, 200), "blue").save(bp1)
    Image.new("RGB", (300, 200), "green").save(bp2)
    r = c.post("/api/batch", files=[
        ("files", ("b1.jpg", open(bp1, "rb").read(), "image/jpeg")),
        ("files", ("b2.jpg", open(bp2, "rb").read(), "image/jpeg")),
    ]).json()
    check("批量-建任务", r["ok"] and r["queued"] == 2, str(r))
    job = None
    for _ in range(60):
        job = c.get(f"/api/batch/{r['job_id']}").json()
        if job["job"]["status"] == "done":
            break
        _time.sleep(0.5)
    check("批量-后台跑完", job["job"]["status"] == "done", str(job["job"]))
    check("批量-1新1重", job["job"]["ok"] == 1 and job["job"]["dup"] == 1,
          f"ok={job['job']['ok']} dup={job['job']['dup']}")
    ing.qr.decode_qr = _real_qr
    ing.webfetch.fetch_page = _real_fetch
    ing.ocr.read_text = _real_ocr   # 全部还原，安全测试用真实现

    r = c.get("/api/qr", params={"text": "hello"})
    check("二维码生成", r.status_code == 200 and r.headers["content-type"] == "image/png")

    code = c.get(f"/api/items/{iid}").json()["item_code"]
    pub = c.get(f"/api/pub/item/{code}").json()
    check("游客导览数据(脱敏)", pub.get("name")
          and "purchase_price" not in pub and "location_id" not in pub,
          str(sorted(pub.keys())))
    check("游客导览页", c.get(f"/pub/item/{code}").status_code == 200)
    check("展签页", c.get(f"/items/{iid}/tag").status_code == 200)

    hr = c.get("/api/health-report").json()
    check("档案体检", all(k in hr for k in ("no_location", "no_front", "dup_photos")),
          str({k: hr[k] for k in ("no_location", "no_front")}))
    check("体检页", c.get("/checkup").status_code == 200)

    ev = c.get("/api/events").json()
    check("全局日志", isinstance(ev, list) and len(ev) >= 1, f"n={len(ev)}")
    check("日志页", c.get("/logs").status_code == 200)
    check("批量页", c.get("/batch").status_code == 200)
    check("录入员批量页", staff.get("/batch").status_code == 200)
    check("只读用户批量被拒", viewer.get("/batch", follow_redirects=False).status_code == 303)

    # ---------- 安全审计修复回归 ----------
    # 1) shot_type 路径穿越拦截（恶意值必须被白名单降级）
    r = c.post(f"/api/items/{iid4}/photos",
               files={"photo": ("x.jpg", buf.getvalue(), "image/jpeg")},
               data={"shot_type": "../../evil"})
    check("安全-上传不报错", r.json().get("ok") is True)
    d = c.get(f"/api/items/{iid4}").json()
    outside = [p for p in d["photos"] if ".." in (p.get("file_path") or "")]
    bad_types = [p for p in d["photos"] if p["shot_type"] not in
                 ("front", "back", "edge", "detail", "label", "holder", "reference")]
    check("安全-shot_type白名单", not outside and not bad_types,
          str([p["shot_type"] for p in d["photos"]]))

    # 2) SSRF：重定向逐跳校验（伪造 302 跳内网必须被拦）
    class _FakeResp:
        is_redirect = True
        status_code = 302
        headers = {"location": "http://127.0.0.1:9000/secret"}
        url = "https://example.com/a"
        def close(self): pass
    class _FakeClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, u): return _FakeResp()
    _orig_client = ing.webfetch.httpx.Client
    ing.webfetch.httpx.Client = _FakeClient
    try:
        check("安全-SSRF重定向拦截",
              ing.webfetch.fetch_page("https://example.com/a") is None)
        check("安全-SSRF图片重定向拦截",
              ing.webfetch.fetch_image("https://example.com/a.jpg") is None)
    finally:
        ing.webfetch.httpx.Client = _orig_client
    check("安全-内网字面量拦截", not ing.webfetch.is_public_url("http://192.168.1.5/x"))
    check("安全-环回拦截", not ing.webfetch.is_public_url("http://localhost/x"))

    # 3) 库位防环
    la = c.post("/api/locations", json={"name": "环测试A"}).json()["id"]
    lb = c.post("/api/locations",
                json={"name": "环测试B", "parent_id": la}).json()["id"]
    check("安全-库位防环", c.put(f"/api/locations/{la}", json={
        "name": "环测试A", "parent_id": lb}).status_code == 400)
    c.delete(f"/api/locations/{lb}")
    c.delete(f"/api/locations/{la}")

    # 4) 安全响应头
    r = c.get("/api/health")
    check("安全-响应头", r.headers.get("x-content-type-options") == "nosniff"
          and "default-src 'self'" in r.headers.get("content-security-policy", ""))

    # 5) 送评字段
    c.post(f"/api/items/{iid}", json={
        "fields": {"send_express": "SF001", "send_fee": 25}})
    d = c.get(f"/api/items/{iid}").json()
    check("送评字段", d.get("send_express") == "SF001" and d.get("send_fee") == 25,
          str((d.get("send_express"), d.get("send_fee"))))

    # ---------- 站点自定义 ----------
    c.post("/api/settings", json={"site_name": "泉缘博物馆", "logo_char": "泉"})
    html = c.get("/login").text
    check("站点自定义-标题", "泉缘博物馆" in html)
    st = c.get("/api/settings").json()
    check("站点自定义-读回", st["site_name"] == "泉缘博物馆"
          and st["logo_char"] == "泉", str(st))

    # ---------- 馆内编号体系：品类码-时代码-年-流水 ----------
    check("字典编码", any(e["name"] == "纸币" and e["code"] == "ZB"
          for e in c.get("/api/dict-all").json()))
    n1 = c.post("/api/items", json={"fields": {
        "category": "纸币", "series": "民国纸币", "name": "编号体系测试一"}}).json()["item_id"]
    c.post(f"/api/items/{n1}", json={"confirm": True})
    code1 = c.get(f"/api/items/{n1}").json()["item_code"]
    check("编号体系-品类时代码", code1.startswith("ZB-MG-2026-"), code1)
    n2 = c.post("/api/items", json={"fields": {
        "category": "纸币", "series": "民国纸币", "name": "编号体系测试二"}}).json()["item_id"]
    c.post(f"/api/items/{n2}", json={"confirm": True})
    code2 = c.get(f"/api/items/{n2}").json()["item_code"]
    check("编号体系-流水递增", code2.startswith("ZB-MG-2026-") and code2 != code1
          and int(code2.split("-")[-1]) == int(code1.split("-")[-1]) + 1,
          f"{code1} → {code2}")
    # 证书号是藏品自身代码，作为属性展示，不承担馆内编号职能
    c.post(f"/api/items/{n1}", json={"fields": {"cert_no": "CERT-ATTR-9"}})
    pub = c.get(f"/api/pub/item/{code1}").json()
    check("证书号作为属性展示", pub.get("cert_no") == "CERT-ATTR-9"
          and "purchase_price" not in pub, str(pub.get("cert_no")))
    # 手动改号：冲突要报错
    r = c.post(f"/api/items/{n2}", json={"fields": {"item_code": code1}}).json()
    check("编号冲突报错", r["ok"] is False and "已被" in r.get("error", ""),
          str(r)[:90])

    # ---------- 本轮优化回归：缩略图 / 筛选导出 / 排序 / 自动备份 ----------
    from app.config import PHOTO_DIR as _PD
    r = c.get("/api/items", params={"limit": 5}).json()
    thumb = next((it["thumb"] for it in r["items"] if it["thumb"]), None)
    check("缩略图生成", thumb is not None and "/thumbs/" in thumb, str(thumb))
    if thumb:
        rel = thumb.split("/media/")[1]
        check("缩略图文件落盘", os.path.exists(os.path.join(str(_PD), rel)), rel)

    r = c.get("/api/export/xlsx", params={"category": "纸币"})
    check("导出-按筛选请求成功", r.status_code == 200 and len(r.content) > 500,
          f"{len(r.content)}B")
    _wb = _load_wb(io.BytesIO(r.content))
    _rows = list(_wb.active.iter_rows(values_only=True))
    _cats = {row[2] for row in _rows[1:]}
    check("导出-筛选语义正确", _cats and all(ca == "纸币" for ca in _cats), str(_cats))

    r = c.get("/api/items", params={"sort": "year", "limit": 50}).json()
    years = [it["year_sort"] for it in r["items"] if it["year_sort"] is not None]
    check("排序-年份升序", years == sorted(years), str(years[:6]))

    from app.backup import run_backup_now
    _bp = run_backup_now()
    check("容器内自动备份", _bp is not None and os.path.exists(_bp), str(_bp))
    d = c.get("/api/health-report").json()
    check("备份统计可见", d.get("db_backups", 0) >= 1, str(d.get("db_backups")))

    # ---------- 馆藏不售出口径 ----------
    c.post(f"/api/items/{n1}", json={"status": "sold"})
    d = c.get(f"/api/items/{n1}").json()
    check("馆藏不售出-sold被拒", d.get("status") != "sold", str(d.get("status")))
    c.post(f"/api/items/{n1}", json={"status": "retired"})
    d = c.get(f"/api/items/{n1}").json()
    check("注销状态可用", d.get("status") == "retired", str(d.get("status")))
    c.post(f"/api/items/{n1}", json={"status": "in_stock"})

    # ---------- 讲解介绍 / 分级 / 目录号 / 估值历史 ----------
    check("等级字典", any(e["name"] == "镇馆之宝"
          for e in c.get("/api/dict/item_level").json()))
    c.post(f"/api/items/{iid}", json={"fields": {
        "level": "镇馆之宝", "catalog_ref": "Pick# 419a", "condition_st": "完好"}})
    d = c.get(f"/api/items/{iid}").json()
    check("分级与目录号", d.get("level") == "镇馆之宝"
          and d.get("catalog_ref") == "Pick# 419a"
          and d.get("condition_st") == "完好",
          str((d.get("level"), d.get("catalog_ref"), d.get("condition_st"))))
    r = c.post(f"/api/items/{iid}/intro/generate").json()
    check("介绍-模板生成", r["ok"] and len(r["text"]) > 5
          and r["source"] == "模板生成", f"{r['source']} {len(r['text'])}字")
    c.post(f"/api/items/{iid}", json={"fields": {"est_value": 5200}})
    d = c.get(f"/api/items/{iid}").json()
    vh = d.get("value_history", [])
    check("估值历史留痕", len(vh) >= 1 and vh[0]["new_value"] == 5200, str(vh[:1]))
    pub = c.get(f"/api/pub/item/{d['item_code']}").json()
    check("导览含介绍与等级", pub.get("intro_text")
          and pub.get("level") == "镇馆之宝", str(pub.get("level")))
    r = c.post(f"/api/items/{iid}/estimate")
    check("AI估值-无插口提示", r.status_code == 400, str(r.status_code))
    r = c.post(f"/api/items/{iid}/intro/tts", json={"voice": "xiaoxiao"})
    if r.status_code == 200 and r.json().get("url"):
        check("TTS语音生成", True, r.json()["url"])
    else:
        print("[SKIP] TTS 生成（外网/微软服务不可达时降级为仅文字）:", r.status_code)

    # ---------- 凭证附件 / 总登记账 / 导览浏览统计 ----------
    r = c.post(f"/api/items/{iid}/attachments",
               files={"file": ("receipt.jpg", buf.getvalue(), "image/jpeg")},
               data={"category": "发票", "notes": "测试购入凭证"})
    check("凭证上传", r.json().get("ok") is True, str(r.json())[:80])
    d = c.get(f"/api/items/{iid}").json()
    check("凭证详情返回", len(d.get("attachments", [])) == 1
          and d["attachments"][0]["category"] == "发票")
    check("凭证对只读隐藏", "attachments" in viewer.get(f"/api/items/{iid}").json()
          and viewer.get(f"/api/items/{iid}").json()["attachments"] == [])
    d2 = c.get(f"/api/items/{iid}").json()
    check("凭证删除", c.delete(f"/api/attachments/{d2['attachments'][0]['id']}").json()["ok"])
    r = c.post(f"/api/items/{iid}/attachments", files={
        "file": ("x.jpg", b"notanimage", "image/jpeg")},
        data={"category": "发票"})
    check("凭证拒非图片", r.status_code == 400, str(r.status_code))

    code_v = c.get(f"/api/items/{iid}").json()["item_code"]
    c.get(f"/api/pub/item/{code_v}")
    c.get(f"/api/pub/item/{code_v}")
    pub = c.get(f"/api/pub/item/{code_v}").json()
    check("导览浏览计数", pub.get("pub_views", 0) >= 3, str(pub.get("pub_views")))
    s = c.get("/api/stats").json()
    check("统计含浏览与凭证", "pub_views" in s and "attachments" in s,
          f"views={s.get('pub_views')}")

    r = c.get("/api/export/register")
    check("总登记账导出", r.status_code == 200 and len(r.content) > 500,
          f"{len(r.content)}B")
    _wb3 = _load_wb(io.BytesIO(r.content))
    _h3 = [c2.value for c2 in next(_wb3.active.iter_rows())]
    check("登记账表头", _h3[:2] == ["登记号", "名称"], str(_h3[:3]))

    # ---------- 真实样本回归（2026-11-18 用户提供的三张标签照片的 OCR 文本） ----------
    # ACG 爱藏：中央银行 1949 伍圆银元券 中华书局版·广州 62EPQ（标签无二维码，走 OCR 路径）
    acg = ("ACG 中央银行 62 EPO 双囍号 1949年伍圆银元券中华书局版·广州地名 "
           "AI CANG GUARANTY 冠号：1-B755211（数四北斗号） Uncireulated")
    f = rules.parse_label_ocr(acg)
    check("真实样本-ACG全字段", f.get("bank") == "中央银行" and f.get("note_type") == "银元券"
          and f.get("printer") == "中华书局" and f.get("grade_raw") == "62 EPQ"
          and f.get("denomination") == "伍圆" and f.get("year_sort") == 1949
          and f.get("serial_no") == "1-B755211" and f.get("series") == "民国纸币",
          str({k: f.get(k) for k in ("bank", "note_type", "grade_raw", "serial_no")}))
    # PMG 正面：粘连 OCR（Pick#431S/M#C304-419495Cents）——宁空勿错
    pmg_stick = ("China,Central Bank of China 53 PMG Pick#431S/M#C304-419495Cents "
                 "PAPERMONEYGUARANTY S/N C072709-Tsingta0 AboutUneireulated")
    f = rules.parse_label_ocr(pmg_stick)
    check("真实样本-PMG粘连", f.get("bank") == "中央银行"
          and f.get("catalog_ref", "").startswith("Pick# 431")
          and f.get("serial_no") == "C072709",
          str({k: f.get(k) for k in ("bank", "catalog_ref", "serial_no")}))
    # PMG 清晰文本：完整解析
    pmg_clean = ("China, Central Bank of China Pick# 431 S/M#C304-4 1949 5 Cents "
                 "S/N C072709 - Tsingtao 53 About Uncirculated")
    f = rules.parse_label_ocr(pmg_clean)
    check("真实样本-PMG清晰", f.get("catalog_ref") == "Pick# 431  S/M#C304-4"
          and f.get("denomination") == "伍分" and f.get("grade_raw") == "53"
          and f.get("grade_scale") == "70制" and f.get("serial_no") == "C072709",
          str({k: f.get(k) for k in ("catalog_ref", "denomination", "grade_raw")}))
    # PMG certlookup URL：Cloudflare 拦页面，但 URL 本身含编号+分数
    f = rules.parse_cert_url("https://www.pmgnotes.com/certlookup/2584605066/45/")
    check("真实样本-certlookup URL", f.get("cert_no") == "2584605066"
          and f.get("grade_raw") == "45" and f.get("grade_scale") == "70制", str(f))
    # 二维码解码增强：右上角裁切放大（PMG 标签小码实测）
    from app.services import qr as _qrsvc
    _p = os.path.join(os.environ["DATA_DIR"], "pmg_qr.jpg")
    qrcode.make("https://www.pmgnotes.com/certlookup/2584605066/45/").save(_p)
    check("二维码-小码策略", _qrsvc.decode_qr(_p) is not None
          and "2584605066" in _qrsvc.decode_qr(_p))

    # ---------- 真实样本回归：华夏评级（2026-11-18 湖南省银行 贰角 1940年 53EPQ） ----------
    hx_ocr = ("湖南省银行 EPO 53 华夏评级 贰角 1940年 HUAXIA COIN GRADING "
              "7578309894 冠号：D0577901 行銀省南湖 535 P 角 可园 D0577901")
    f = rules.parse_label_ocr(hx_ocr)
    check("真实样本-华夏全字段",
          f.get("category") == "纸币" and f.get("bank") == "湖南省银行"
          and f.get("series") == "民国纸币" and f.get("grade_co") == "华夏"
          and f.get("grade_raw") == "53 EPQ" and f.get("grade_scale") == "70制"
          and f.get("denomination") == "贰角" and f.get("year_sort") == 1940
          and f.get("serial_no") == "D0577901" and f.get("cert_no") == "7578309894",
          str({k: f.get(k) for k in ("grade_co", "cert_no", "serial_no", "grade_raw", "denomination")}))
    # 古钱「82分」分数不可被「分」面值规则误吞
    f_coin = rules.parse_label_ocr("公博评级 乾隆通宝 82分")
    check("面值防误吞85制分数",
          f_coin.get("grade_raw") == "82分" and f_coin.get("grade_scale") == "85制"
          and not f_coin.get("denomination"), str(f_coin))
    # 华夏三套二维码 URL 结构与域名识别
    from app.adapters.registry import company_for_url
    check("真实样本-华夏URL提取",
          rules.parse_cert_url("http://huaxiapj.com/c/7578309894").get("cert_no") == "7578309894"
          and rules.parse_cert_url("https://www.huaxiapj.com/querySendItem.html?orderNum=7578309894").get("cert_no") == "7578309894"
          and company_for_url("https://z.wpt.la/218/219/12345678901") == "华夏")
    # 华夏 SPA 空壳页拦截回归：返回 200 但正文为 {{QueryResult.xxx}} 时必须走 OCR 且不抓占位图
    ing.qr.decode_qr = lambda p: "http://huaxiapj.com/c/7578309894"
    ing.webfetch.fetch_page = lambda u: {
        "url_final": "https://www.huaxiapj.com/querySendItem.html?orderNum=7578309894",
        "html": ("<html><body>名称 {{QueryResult.name}} 证书编号 {{PjNumber}} "
                 "<img src='https://www.huaxiapj.com/images/test_coin_123.jpg'></body></html>")}
    ing.ocr.read_text = lambda p: hx_ocr
    try:
        res_hx = ing.ingest_label(open(plain, "rb").read())
        check("流水线-SPA空壳降级OCR且保URL编号",
              res_hx["ok"] and res_hx["parsed_from"] == "ocr_label"
              and res_hx["ref_photos"] == 0
              and res_hx["fields"].get("cert_no") == "7578309894"
              and res_hx["fields"].get("grade_co") == "华夏"
              and res_hx["fields"].get("denomination") == "贰角",
              str({k: res_hx.get(k) for k in ("parsed_from", "ref_photos", "cert_url")}))
    finally:
        ing.qr.decode_qr = _real_qr
        ing.webfetch.fetch_page = _real_fetch
        ing.ocr.read_text = _real_ocr

print(f"\n== 冒烟测试完成：{ok_count} 项通过 ==")
