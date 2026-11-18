"""识别流水线编排：拍标签 → 二维码/OCR → 抓证书页 → 规则解析 → 草稿档案。

照片规则（用户确认）：
- 证书页有照片 → 下载为「参考图」；
- 没有照片 → 用现场拍的正反面「档案图」（正反面都要拍）；
- 都没有/识别不出 → 手动添加修改，字段宁空勿错。
"""
import json
from datetime import datetime
from typing import Optional
from uuid import uuid4

from . import models
from .adapters import rules, webfetch
from .adapters.registry import company_for_url, is_non_cert_url, lookup_url_for
from .config import PHOTO_DIR
from .models import get_conn
from .services import ocr, photos, qr, years

# 确认时允许写入的字段白名单（与 item 表列一一对应）
FIELD_WHITELIST = [
    "item_code",
    "category", "dynasty", "bank", "note_type", "series", "printer", "doc_type",
    "name", "era", "denomination", "variety", "material",
    "year_text", "region", "grade_co", "cert_no", "cert_url",
    "grade_raw", "grade_scale", "serial_no",
    "source", "purchase_price", "purchase_date", "est_value", "est_date", "notes",
    "send_express", "send_fee",
    "level", "catalog_ref", "condition_st", "intro_text",
]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ingest_label(image_bytes: bytes) -> dict:
    """处理一张评级标签照片，返回识别结果与草稿 item_id。"""
    tmp_dir = PHOTO_DIR / "_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp = tmp_dir / f"{uuid4().hex}.jpg"
    tmp.write_bytes(image_bytes)
    try:
        fields: dict = {}
        parsed_from, cert_url, page_html, ocr_text = "none", None, None, None
        qr_url = qr.decode_qr(tmp)
        # 过滤背面公众号关注码（如闻德背面 weixin.qq.com/r/...），不作证书查询链接
        if qr_url and "weixin.qq.com/" in qr_url.lower():
            qr_url = None

        with get_conn() as conn:
            # ① 二维码 → 证书页抓取 → 规则解析
            #    微信分享码等非证书域名直接跳过抓取（纯 OCR）
            if qr_url and is_non_cert_url(qr_url):
                qr_url = None

            if qr_url and qr_url.startswith("http"):
                # URL 结构本身可携带字段（PMG certlookup/{编号}/{分数}、华夏 /c/{编号}——
                # 页面有 Cloudflare 或为前端 SPA 空壳时，URL 拦不住）
                url_fields = rules.parse_cert_url(qr_url)
                page = webfetch.fetch_page(qr_url)
                if page:
                    final_u = page["url_final"]
                    for k, v in rules.parse_cert_url(final_u).items():
                        url_fields.setdefault(k, v)
                    # 若被重定向到登录跳板页（如 7788 Localstoragelogin），保留原始二维码链接更直观
                    if ("login" in final_u.lower() or "signin" in final_u.lower()) and webfetch.is_public_url(qr_url):
                        cert_url = qr_url
                    else:
                        cert_url = final_u
                    text = webfetch.extract_text(page["html"])
                    if webfetch.is_spa_shell(text, page["html"]):
                        # 华夏等前端 Vue 空壳页（返回 200 但全是 {{QueryResult.xxx}}）：
                        # 不抓空壳里的占位装饰图，退回 URL 字段 + 标签 OCR 合并
                        fields.update(url_fields)
                        parsed_from = "qr_url" if url_fields else "none"
                        conn.execute(
                            "INSERT INTO cert_fetch(item_id, adapter, url, ok, message) VALUES(?,?,?,?,?)",
                            (None, "rules", cert_url, 0, "前端动态渲染空壳"
                             + ("，字段取自URL" if url_fields else "")))
                    else:
                        page_html = page["html"]
                        fields = rules.parse_cert_page(text, cert_url)
                        for k, v in url_fields.items():
                            fields.setdefault(k, v)
                        parsed_from = "qr_cert"
                        conn.execute(
                            "INSERT INTO cert_fetch(item_id, adapter, url, ok, message) VALUES(?,?,?,?,?)",
                            (None, "rules", cert_url, 1, f"文本{len(text)}字"))
                else:
                    if webfetch.is_public_url(qr_url):
                        cert_url = qr_url
                    fields.update(url_fields)
                    parsed_from = "qr_url" if url_fields else "none"
                    conn.execute(
                        "INSERT INTO cert_fetch(item_id, adapter, url, ok, message) VALUES(?,?,?,?,?)",
                        (None, "rules", qr_url, 0, "抓取失败或非公网地址"
                         + ("，字段取自URL" if url_fields else "")))

            # ② OCR 兜底（二维码读不出、页面 403 或前端空壳）；
            #    OCR 字段优先（更具体），但 cert_no/grade 等 URL 结构化字段
            #    不可被 OCR 的猜测值覆盖（实测：条码数字会被截断误认、冠字号易混淆）
            if parsed_from in ("none", "qr_url") or not fields:
                ocr_text = ocr.read_text(tmp)
                if ocr_text:
                    ocr_fields = rules.parse_label_ocr(ocr_text)
                    merged = {k: v for k, v in fields.items() if v}
                    for k, v in ocr_fields.items():
                        if v:
                            merged[k] = v
                    for k in ("cert_no", "grade_raw", "grade_scale"):
                        if fields.get(k):
                            merged[k] = fields[k]
                    fields = merged
                    parsed_from = "ocr_label"

            # ③ 评级编号重复拦截（防同一件币录两次）
            cert_no = fields.get("cert_no")
            if cert_no:
                dup = conn.execute(
                    "SELECT id, item_code, name FROM item WHERE cert_no=?",
                    (cert_no,)).fetchone()
                if dup:
                    return {"ok": False, "dupe": True, "cert_no": cert_no,
                            "existing": dict(dup)}

            # ③.5 域名台账 → 自动识别评级公司（URL 域名权威优先于 OCR 文本猜测）
            url_for_co = cert_url or (qr_url if qr_url and qr_url.startswith("http") else None)
            if url_for_co:
                gc = company_for_url(url_for_co)
                if gc:
                    fields["grade_co"] = gc
            elif fields.get("grade_co") == "PCGS" and fields.get("cert_no"):
                cert_url = f"https://www.pcgs.com/banknotecert/{fields['cert_no']}"

            # ④ 建草稿档案
            cur = conn.execute(
                """INSERT INTO item(status, cert_no, cert_url, grade_co, grade_raw, grade_scale, name,
                    category, dynasty, bank, note_type, series, printer, doc_type,
                    denomination, era, variety, year_text, year_sort, serial_no, catalog_ref, notes)
                    VALUES('draft',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fields.get("cert_no"), cert_url, fields.get("grade_co"),
                 fields.get("grade_raw"), fields.get("grade_scale"), fields.get("name"),
                 fields.get("category"), fields.get("dynasty"), fields.get("bank"),
                 fields.get("note_type"), fields.get("series"), fields.get("printer"),
                 fields.get("doc_type"), fields.get("denomination"), fields.get("era"),
                 fields.get("variety"), fields.get("year_text"), fields.get("year_sort"),
                 fields.get("serial_no"), fields.get("catalog_ref"), None))
            item_id = cur.lastrowid
            models.fts_sync(conn, item_id, fields)
            conn.execute(
                "INSERT INTO recognition(item_id, engine, raw_json) VALUES(?,?,?)",
                (item_id, parsed_from, json.dumps(
                    {"fields": fields, "qr_url": qr_url,
                     "page_text": (webfetch.extract_text(page_html) if page_html else ocr_text or "")[:2000]},
                    ensure_ascii=False)))
            if ocr_text:
                conn.execute("UPDATE item SET notes=? WHERE id=?",
                             (f"标签OCR原文：{ocr_text[:200]}", item_id))

            # ⑤ 证书页有照片 → 下载原图存为「参考图」（最多 2 张）
            ref_saved = 0
            ref_urls = []
            if page_html and cert_url:
                for img_url in webfetch.extract_image_urls(page_html, cert_url):
                    if ref_saved >= 2:
                        break
                    img = webfetch.fetch_image(img_url)
                    if not img:
                        continue
                    meta = photos.save_image(img, item_id, "reference", origin="reference")
                    if meta:
                        conn.execute(
                            """INSERT INTO photo(item_id, shot_type, origin, source_url,
                               fetched_at, file_path, phash) VALUES(?,?,?,?,?,?,?)""",
                            (item_id, "reference", "reference", img_url,
                             _now(), meta["file_path"], meta["phash"]))
                        ref_saved += 1
                        ref_urls.append(f"/media/{meta['file_path']}")

        # 识别质量评估：实质字段极少且无编号 → 提示前端归并为上一件的补充照片
        # （员工逐件拍正/背两张标签，第二张常只是公司 logo 面）
        quality = sum(1 for k in ("name", "bank", "denomination", "year_text",
                                  "grade_raw", "serial_no", "catalog_ref",
                                  "cert_no") if fields.get(k))
        if quality < 2 and not fields.get("cert_no"):
            return {"ok": False, "low_quality": True,
                    "grade_co": fields.get("grade_co"),
                    "hint": "这张照片识别信息少，已作为上一件的补充照片保存"}

        lookup_url = None
        if parsed_from != "qr_cert":
            lookup_url = lookup_url_for(fields.get("grade_co"))

        return {"ok": True, "item_id": item_id, "fields": fields,
                "parsed_from": parsed_from, "cert_url": cert_url,
                "ref_photos": ref_saved, "ref_photo_urls": ref_urls,
                "qr_url": qr_url, "lookup_url": lookup_url}
    finally:
        tmp.unlink(missing_ok=True)


def attach_photo(item_id: int, data: bytes, shot_type: str) -> dict:
    """给藏品挂现场照片（档案图），顺带做相似度查重提示。"""
    with get_conn() as conn:
        if not conn.execute("SELECT id FROM item WHERE id=?", (item_id,)).fetchone():
            return {"ok": False, "error": "藏品不存在"}
        meta = photos.save_image(data, item_id, shot_type, origin="archive")
        if not meta:
            return {"ok": False, "error": "图片无法识别"}
        similar = photos.find_similar(meta["phash"], item_id, conn=conn)
        conn.execute(
            """INSERT INTO photo(item_id, shot_type, origin, file_path, exif_time, phash)
               VALUES(?,?,?,?,?,?)""",
            (item_id, meta["shot_type"], meta["origin"], meta["file_path"],
             meta["exif_time"], meta["phash"]))
        return {"ok": True, "similar": similar}


def create_manual(fields: dict) -> int:
    """手动建档（员工模式"手动录入"）。"""
    fields = {k: fields.get(k) for k in FIELD_WHITELIST}
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO item(status, {', '.join(FIELD_WHITELIST)}) "
            f"VALUES('draft', {', '.join('?' * len(FIELD_WHITELIST))})",
            [fields[k] for k in FIELD_WHITELIST])
        item_id = cur.lastrowid
        conn.execute("INSERT INTO recognition(item_id, engine, raw_json) VALUES(?,?,?)",
                     (item_id, "manual", "{}"))
        models.fts_sync(conn, item_id, fields)
        return item_id


# 馆藏定位：本系统藏品永不售出（销售走独立的自动售货机体系）。
# 藏品退出馆藏走「注销」（retired），需留痕；sold/consigned 仅作历史数据显示。
VALID_STATUS = ["draft", "in_stock", "reviewing", "exhibited",
                "lent", "lost", "retired"]
_NUMERIC_FIELDS = ("purchase_price", "est_value", "send_fee")


def _dict_code(conn, dtype: str, name) -> Optional[str]:
    """从字典取品类/时代编码（编号体系用）。"""
    if not name:
        return None
    r = conn.execute("SELECT code FROM dict_entry WHERE type=? AND name=?",
                     (dtype, name)).fetchone()
    return (r["code"] or "").strip() or None if r else None


def _gen_item_code(conn, category, series, dynasty) -> str:
    """馆内编号：品类码-时代码-入藏年-流水号，如 ZB-MG-2026-0012。
    品类/时代码挂在字典上（可后台改）；编号生成后不随分类修改而变化。"""
    cat = _dict_code(conn, "category", category) or "QT"
    era = (_dict_code(conn, "series", series)
           or _dict_code(conn, "dynasty", dynasty) or "XX")
    year = datetime.now().year
    base = f"{cat}-{era}-{year}-"
    n = conn.execute("SELECT COUNT(*) FROM item WHERE item_code LIKE ?",
                     (base + "%",)).fetchone()[0] + 1
    while True:
        code = f"{base}{n:04d}"
        if not conn.execute("SELECT 1 FROM item WHERE item_code=?",
                            (code,)).fetchone():
            return code
        n += 1


def confirm_item(item_id: int, fields: dict, confirm: bool,
                 operator: Optional[str] = None, status: Optional[str] = None,
                 location_id: Optional[int] = None) -> dict:
    """确认入库 / 保存修改（编辑页用）。确认时生成一物一码；支持状态流转与库位变更。"""
    fields = {k: v for k, v in (fields or {}).items() if k in FIELD_WHITELIST}
    for k in _NUMERIC_FIELDS:
        if k in fields:
            try:
                fields[k] = float(fields[k]) if fields[k] not in ("", None) else None
            except (TypeError, ValueError):
                fields[k] = None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, item_code, status, location_id, category, series, dynasty, "
            "bank, note_type, denomination, era, name, est_value "
            "FROM item WHERE id=?", (item_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "藏品不存在"}

        # 手动改号：先校验唯一，改完留痕
        if fields.get("item_code") and fields["item_code"] != row["item_code"]:
            dupc = conn.execute(
                "SELECT id FROM item WHERE item_code=? AND id<>?",
                (fields["item_code"], item_id)).fetchone()
            if dupc:
                return {"ok": False,
                        "error": f"编号 {fields['item_code']} 已被另一件藏品使用"}

        sets, vals = [], []
        for k, v in fields.items():
            sets.append(f"{k}=?")
            vals.append(v)
        # 纪年 → 排序年（员工不用懂民国/年号纪年）
        yt = fields.get("year_text")
        if yt is not None and not fields.get("year_sort"):
            g = years.to_gregorian(str(yt))
            if g:
                sets.append("year_sort=?")
                vals.append(g)
        # 名称没填则自动拼一个
        if not fields.get("name"):
            parts = [fields.get("bank") or row["bank"],
                     fields.get("note_type") or row["note_type"],
                     fields.get("denomination") or row["denomination"],
                     fields.get("era") or row["era"]]
            name = " ".join(str(p) for p in parts if p)
            if name:
                sets.append("name=?")
                vals.append(name)

        # 状态流转
        new_status = None
        if confirm:
            new_status = status if status in VALID_STATUS else "in_stock"
        elif status in VALID_STATUS and status != row["status"]:
            new_status = status
        if new_status:
            sets.append("status=?")
            vals.append(new_status)

        # 库位变更
        if location_id is not None:
            loc_id = None if location_id in (0, "") else location_id
            if loc_id is not None and not conn.execute(
                    "SELECT id FROM location WHERE id=?", (loc_id,)).fetchone():
                loc_id = None
            if loc_id != row["location_id"]:
                sets.append("location_id=?")
                vals.append(loc_id)
                conn.execute(
                    "INSERT INTO event(item_id, kind, detail, operator) VALUES(?,?,?,?)",
                    (item_id, "位置变更",
                     f"{row['location_id'] or '无'} → {loc_id or '无'}", operator))

        # 估值变更留痕（估值历史）
        if "est_value" in fields and fields["est_value"] is not None:
            new_v, old_v = fields["est_value"], row["est_value"]
            if old_v is None or abs(new_v - old_v) > 1e-9:
                conn.execute(
                    "INSERT INTO value_history(item_id, old_value, new_value, "
                    "source, operator) VALUES(?,?,?,?,?)",
                    (item_id, old_v, new_v, "手动", operator))

        # 状态改为在库时补发一物一码（馆内编号体系：品类码-时代码-入藏年-流水）
        item_code = fields.get("item_code") or row["item_code"]
        if (confirm or new_status == "in_stock") and not item_code:
            item_code = _gen_item_code(
                conn,
                fields.get("category") or row["category"],
                fields.get("series") or row["series"],
                fields.get("dynasty") or row["dynasty"])
            sets.append("item_code=?")
            vals.append(item_code)
        # 手动改号留痕
        if fields.get("item_code") and fields["item_code"] != row["item_code"]:
            conn.execute(
                "INSERT INTO event(item_id, kind, detail, operator) VALUES(?,?,?,?)",
                (item_id, "编号变更",
                 f"{row['item_code'] or '（无）'} → {fields['item_code']}", operator))

        if sets:
            sets.append("updated_at=datetime('now','localtime')")
            conn.execute(f"UPDATE item SET {', '.join(sets)} WHERE id=?",
                         [*vals, item_id])
            row2 = conn.execute(
                "SELECT name, notes, region, variety, era, denomination, cert_no, "
                "item_code, intro_text, catalog_ref "
                "FROM item WHERE id=?", (item_id,)).fetchone()
            models.fts_sync(conn, item_id, dict(row2))
        if new_status and new_status != row["status"]:
            detail = f"{row['status']} → {new_status}"
            if confirm and item_code:
                detail += f"，编号 {item_code}"
            conn.execute(
                "INSERT INTO event(item_id, kind, detail, operator) VALUES(?,?,?,?)",
                (item_id, "入库" if confirm else "状态变更", detail, operator))
        return {"ok": True, "item_code": item_code if confirm else None}
