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
shutil.rmtree(os.environ["DATA_DIR"], ignore_errors=True)

from PIL import Image  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
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

print(f"\n== 冒烟测试完成：{ok_count} 项通过 ==")
