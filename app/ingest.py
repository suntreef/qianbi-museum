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
from .adapters.registry import company_for_url
from .config import ITEM_CODE_PREFIX, PHOTO_DIR
from .models import get_conn
from .services import ocr, photos, qr, years

# 确认时允许写入的字段白名单（与 item 表列一一对应）
FIELD_WHITELIST = [
    "category", "dynasty", "bank", "note_type", "series", "printer", "doc_type",
    "name", "era", "denomination", "variety", "material",
    "year_text", "region", "grade_co", "cert_no", "cert_url",
    "grade_raw", "grade_scale", "serial_no",
    "source", "purchase_price", "purchase_date", "est_value", "est_date", "notes",
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

        with get_conn() as conn:
            # ① 二维码 → 抓证书页 → 规则解析
            if qr_url and qr_url.startswith("http"):
                page = webfetch.fetch_page(qr_url)
                if page:
                    page_html = page["html"]
                    cert_url = page["url_final"]
                    text = webfetch.extract_text(page_html)
                    fields = rules.parse_cert_page(text, cert_url)
                    parsed_from = "qr_cert"
                    conn.execute(
                        "INSERT INTO cert_fetch(item_id, adapter, url, ok, message) VALUES(?,?,?,?,?)",
                        (None, "rules", cert_url, 1, f"文本{len(text)}字"))
                else:
                    conn.execute(
                        "INSERT INTO cert_fetch(item_id, adapter, url, ok, message) VALUES(?,?,?,?,?)",
                        (None, "rules", qr_url, 0, "抓取失败或非公网地址"))

            # ② OCR 兜底（二维码读不出或页面没抓到）
            if parsed_from == "none":
                ocr_text = ocr.read_text(tmp)
                if ocr_text:
                    fields = rules.parse_label_ocr(ocr_text)
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

            # ③.5 域名台账 → 自动识别评级公司
            if cert_url and not fields.get("grade_co"):
                gc = company_for_url(cert_url)
                if gc:
                    fields["grade_co"] = gc

            # ④ 建草稿档案
            cur = conn.execute(
                """INSERT INTO item(status, cert_no, cert_url, grade_co, grade_raw, grade_scale, name,
                    category, dynasty, bank, note_type, series, printer, doc_type,
                    denomination, era, variety, year_text, year_sort, serial_no, notes)
                    VALUES('draft',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fields.get("cert_no"), cert_url, fields.get("grade_co"),
                 fields.get("grade_raw"), fields.get("grade_scale"), fields.get("name"),
                 fields.get("category"), fields.get("dynasty"), fields.get("bank"),
                 fields.get("note_type"), fields.get("series"), fields.get("printer"),
                 fields.get("doc_type"), fields.get("denomination"), fields.get("era"),
                 fields.get("variety"), fields.get("year_text"), fields.get("year_sort"),
                 fields.get("serial_no"), None))
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

        return {"ok": True, "item_id": item_id, "fields": fields,
                "parsed_from": parsed_from, "cert_url": cert_url,
                "ref_photos": ref_saved, "ref_photo_urls": ref_urls, "qr_url": qr_url}
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


VALID_STATUS = ["draft", "in_stock", "reviewing", "exhibited",
                "lent", "consigned", "sold", "lost"]
_NUMERIC_FIELDS = ("purchase_price", "est_value")


def confirm_item(item_id: int, fields: dict, confirm: bool,
                 operator: Optional[str] = None, status: Optional[str] = None) -> dict:
    """确认入库 / 保存修改（编辑页用）。确认时生成一物一码；支持状态流转。"""
    fields = {k: v for k, v in (fields or {}).items() if k in FIELD_WHITELIST}
    for k in _NUMERIC_FIELDS:
        if k in fields:
            try:
                fields[k] = float(fields[k]) if fields[k] not in ("", None) else None
            except (TypeError, ValueError):
                fields[k] = None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, item_code, status, bank, note_type, denomination, era, name "
            "FROM item WHERE id=?", (item_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "藏品不存在"}

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

        item_code = row["item_code"]
        if confirm and not item_code:
            item_code = f"{ITEM_CODE_PREFIX}-{datetime.now().year}-{item_id:04d}"
            sets.append("item_code=?")
            vals.append(item_code)

        if sets:
            sets.append("updated_at=datetime('now','localtime')")
            conn.execute(f"UPDATE item SET {', '.join(sets)} WHERE id=?",
                         [*vals, item_id])
            row2 = conn.execute(
                "SELECT name, notes, region, variety, era, denomination "
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
