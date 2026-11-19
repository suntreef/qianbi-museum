"""JSON API：录入、照片、确认、检索、字典、用户、统计、导出。

权限矩阵：
- admin：全部（用户管理、字典写、Excel 导出、删档案、价格/估值可见）
- staff：采集录入、改档案（价格字段被服务端过滤）、删除照片
- viewer：只读浏览（价格不可见），写接口一律 403
"""
import io
import os
import sqlite3 as _sqlite3
import tempfile as _tempfile
from datetime import datetime as _dt
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter
from PIL import Image as _PILImage
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import models
from .services import llm as services_llm
from .auth import COOKIE, hash_password, login_user, make_token, require_role
from .config import BATCH_DIR, COOKIE_SECURE, DATA_DIR, PHOTO_DIR, PUB_TOKEN
from .ingest import attach_photo, confirm_item, create_manual, ingest_label
from .models import get_conn, get_setting, set_setting
from .services import photos

router = APIRouter(prefix="/api")

require_any = require_role()                    # 任何已登录用户
require_staff = require_role("admin", "staff")  # 录入及以上
require_admin = require_role("admin")           # 仅管理员

STATUS_CN = {"draft": "草稿", "in_stock": "在库", "reviewing": "送评中",
             "exhibited": "展出中", "lent": "借出", "retired": "注销",
             "consigned": "寄售(历史)", "sold": "已售(历史)", "lost": "丢失"}

EXPORT_COLS = [
    ("item_code", "内部编号"), ("name", "名称"), ("category", "品类"),
    ("series", "系列"), ("bank", "发行机构"), ("note_type", "券种/类型"),
    ("printer", "印刷厂"), ("doc_type", "文书类型"), ("dynasty", "朝代"),
    ("era", "年号/品种"), ("denomination", "面值"), ("variety", "版别"),
    ("material", "材质"), ("year_text", "纪年"), ("year_sort", "公历年"),
    ("region", "地域"), ("grade_co", "评级公司"), ("cert_no", "评级编号"),
    ("grade_raw", "分数"), ("grade_scale", "分数制"), ("serial_no", "冠字号"),
    ("level", "藏品等级"), ("catalog_ref", "目录号"), ("condition_st", "保存状况"),
    ("status", "状态"), ("source", "来源"), ("purchase_price", "采购价"),
    ("purchase_date", "采购日期"), ("est_value", "估值"), ("est_date", "估值日期"),
    ("send_express", "送评单号"), ("send_fee", "送评费用"),
    ("notes", "备注"), ("created_at", "录入时间"),
]

FINANCIAL = ("purchase_price", "est_value")
UPLOAD_MAX = 16 * 1024 * 1024
MAX_BATCH_BYTES = 600 * 1024 * 1024   # 单次批量上传总大小上限

_SORTS = {
    "": "id DESC", "new": "id DESC", "old": "id ASC",
    "year": "year_sort IS NULL, year_sort ASC",
    "year_desc": "year_sort IS NULL, year_sort DESC",
    "value": "est_value IS NULL, est_value DESC",
    "code": "item_code ASC",
}


def _item_filters(conn, q, category, series, note_type, bank, printer, doc_type,
                  dynasty, status, level, location_id, year_from, year_to):
    """检索条件构造（列表检索与 Excel 导出共用一套语义）。
    返回 (where, vals)；q 命中全文索引且为空时返回 None（确认无结果）。"""
    where, vals = ["1=1"], []
    if q:
        ids = models.fts_search_ids(conn, q)
        if ids is not None:
            if not ids:
                return None
            where.append(f"id IN ({','.join('?' * len(ids))})")
            vals.extend(ids)
        else:
            like = f"%{q}%"
            cols = ("name", "era", "cert_no", "item_code", "region", "variety",
                    "notes", "denomination", "serial_no", "bank", "note_type",
                    "intro_text", "catalog_ref")
            where.append("(" + " OR ".join(f"{c} LIKE ?" for c in cols) + ")")
            vals.extend([like] * len(cols))
    for col, v in (("category", category), ("series", series),
                   ("note_type", note_type), ("bank", bank), ("printer", printer),
                   ("doc_type", doc_type), ("dynasty", dynasty), ("status", status),
                   ("level", level)):
        if v:
            where.append(f"{col} = ?")
            vals.append(v)
    if year_from is not None:
        where.append("year_sort >= ?")
        vals.append(year_from)
    if year_to is not None:
        where.append("year_sort <= ?")
        vals.append(year_to)
    if location_id is not None:
        if location_id == -1:
            where.append("location_id IS NULL")
        else:
            where.append("location_id = ?")
            vals.append(location_id)
    return where, vals


def _strip_financial(d: dict, role: str) -> dict:
    """非管理员看不到采购价与估值。"""
    if role != "admin":
        for k in FINANCIAL:
            d.pop(k, None)
    return d


def _json(data: dict):
    from fastapi.responses import JSONResponse
    return JSONResponse(data)


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---------- 登录 / 当前用户 ----------

@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    user = login_user(_client_ip(request), username.strip(), password)
    if not user:
        raise HTTPException(401, "用户名或口令不对（连续失败将临时锁定）")
    resp = _json({"ok": True, "role": user["role"]})
    resp.set_cookie(COOKIE, make_token(user["id"]), httponly=True,
                    samesite="lax", max_age=30 * 86400, secure=COOKIE_SECURE)
    return resp


@router.get("/me")
def me(user: dict = Depends(require_any)):
    return {"ok": True, "username": user["username"], "role": user["role"]}


@router.post("/logout")
def logout():
    resp = _json({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


# ---------- 用户管理（仅管理员） ----------

class UserPayload(BaseModel):
    username: str = ""
    password: Optional[str] = None
    role: str = "staff"
    disabled: Optional[bool] = None


@router.get("/users")
def list_users(_: dict = Depends(require_admin)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, username, role, disabled, created_at FROM users ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/users")
def add_user(p: UserPayload, admin: dict = Depends(require_admin)):
    if p.role not in ("admin", "staff", "viewer"):
        raise HTTPException(400, "角色不对")
    if not p.username.strip() or not p.password:
        raise HTTPException(400, "用户名和口令必填")
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO users(username, password_hash, role) VALUES(?,?,?)",
                (p.username.strip(), hash_password(p.password), p.role))
        except Exception:
            raise HTTPException(400, "用户名已存在")
    return {"ok": True}


@router.put("/users/{user_id}")
def update_user(user_id: int, p: UserPayload, admin: dict = Depends(require_admin)):
    with get_conn() as conn:
        if not conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone():
            raise HTTPException(404, "用户不存在")
        if p.role:
            if p.role not in ("admin", "staff", "viewer"):
                raise HTTPException(400, "角色不对")
            if user_id == admin["id"] and p.role != "admin":
                raise HTTPException(400, "不能把自己的管理员权限降掉")
            conn.execute("UPDATE users SET role=? WHERE id=?", (p.role, user_id))
        if p.disabled is not None:
            if user_id == admin["id"] and p.disabled:
                raise HTTPException(400, "不能停用自己")
            conn.execute("UPDATE users SET disabled=? WHERE id=?",
                         (1 if p.disabled else 0, user_id))
        if p.password:
            conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                         (hash_password(p.password), user_id))
    return {"ok": True}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, admin: dict = Depends(require_admin)):
    if user_id == admin["id"]:
        raise HTTPException(400, "不能删除自己")
    with get_conn() as conn:
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    return {"ok": True}


# ---------- 字典 ----------

DICT_TYPES = ("category", "dynasty", "bank", "note_type", "series",
              "printer", "doc_type", "grade_co", "material", "cert_domain",
              "item_level", "condition_st")


class DictPayload(BaseModel):
    type: str
    name: str
    aliases: Optional[str] = None
    sort: int = 50
    code: Optional[str] = None   # 品类/时代编码（编号体系用）


@router.get("/dict/{dict_type}")
def get_dict(dict_type: str, _: dict = Depends(require_any)):
    if dict_type not in DICT_TYPES:
        raise HTTPException(404, "未知字典类型")
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT name, aliases FROM dict_entry WHERE type=? ORDER BY sort, id",
            (dict_type,)).fetchall()
    return [{"name": r["name"], "aliases": r["aliases"]} for r in rows]


@router.get("/dict-all")
def dict_all(_: dict = Depends(require_any)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM dict_entry ORDER BY type, sort, id").fetchall()
    return [dict(r) for r in rows]


@router.post("/dict")
def add_dict(p: DictPayload, _: dict = Depends(require_admin)):
    if p.type not in DICT_TYPES:
        raise HTTPException(400, "未知字典类型")
    if not p.name.strip():
        raise HTTPException(400, "名称不能为空")
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO dict_entry(type, name, aliases, sort, code) VALUES(?,?,?,?,?)
               ON CONFLICT(type, name) DO UPDATE SET
               aliases=excluded.aliases, sort=excluded.sort, code=excluded.code""",
            (p.type, p.name.strip(), p.aliases, p.sort, (p.code or "").strip() or None))
    return {"ok": True}


@router.put("/dict/{entry_id}")
def update_dict(entry_id: int, p: DictPayload, _: dict = Depends(require_admin)):
    if p.type not in DICT_TYPES or not p.name.strip():
        raise HTTPException(400, "参数不对")
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE dict_entry SET type=?, name=?, aliases=?, sort=?, code=? WHERE id=?",
            (p.type, p.name.strip(), p.aliases, p.sort,
             (p.code or "").strip() or None, entry_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "条目不存在")
    return {"ok": True}


@router.delete("/dict/{entry_id}")
def delete_dict(entry_id: int, _: dict = Depends(require_admin)):
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM dict_entry WHERE id=?", (entry_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "条目不存在")
    return {"ok": True}


# ---------- 采集（识别流水线） ----------

@router.post("/ingest/label")
async def ingest_label_ep(photo: UploadFile = File(...),
                          user: dict = Depends(require_staff)):
    if photo.size and photo.size > UPLOAD_MAX:
        raise HTTPException(413, "图片太大（限 16MB）")
    data = await photo.read()
    if not data:
        raise HTTPException(400, "空文件")
    if len(data) > UPLOAD_MAX:
        raise HTTPException(413, "图片太大（限 16MB）")
    return await run_in_threadpool(ingest_label, data)


@router.post("/items/{item_id}/photos")
async def upload_photo(item_id: int, photo: UploadFile = File(...),
                       shot_type: str = Form("front"),
                       user: dict = Depends(require_staff)):
    if photo.size and photo.size > UPLOAD_MAX:
        raise HTTPException(413, "图片太大（限 16MB）")
    data = await photo.read()
    if not data or len(data) > UPLOAD_MAX:
        raise HTTPException(400, "空文件或图片太大")
    return await run_in_threadpool(attach_photo, item_id, data, shot_type)


# ---------- 藏品 ----------

class ItemPayload(BaseModel):
    fields: dict = {}
    confirm: bool = False
    status: Optional[str] = None
    location_id: Optional[int] = None


@router.post("/items")
async def create_item_ep(payload: ItemPayload, user: dict = Depends(require_staff)):
    fields = dict(payload.fields or {})
    if user["role"] != "admin":
        for k in FINANCIAL:
            fields.pop(k, None)
    item_id = await run_in_threadpool(create_manual, fields)
    return {"ok": True, "item_id": item_id}


@router.post("/items/{item_id}")
async def update_item_ep(item_id: int, payload: ItemPayload,
                         user: dict = Depends(require_staff)):
    fields = dict(payload.fields or {})
    if user["role"] != "admin":
        for k in FINANCIAL:
            fields.pop(k, None)   # 非管理员的改价直接丢弃
    return await run_in_threadpool(
        confirm_item, item_id, fields, payload.confirm,
        user["username"], payload.status, payload.location_id)


@router.get("/items")
def search_items(request: Request, q: Optional[str] = None,
                 category: Optional[str] = None, series: Optional[str] = None,
                 note_type: Optional[str] = None, bank: Optional[str] = None,
                 printer: Optional[str] = None, doc_type: Optional[str] = None,
                 dynasty: Optional[str] = None, status: Optional[str] = None,
                 level: Optional[str] = None, location_id: Optional[int] = None,
                 year_from: Optional[int] = None, year_to: Optional[int] = None,
                 sort: Optional[str] = None,
                 limit: int = 50, offset: int = 0,
                 user: dict = Depends(require_any)):
    with get_conn() as conn:
        f = _item_filters(conn, q, category, series, note_type, bank, printer,
                          doc_type, dynasty, status, level, location_id,
                          year_from, year_to)
        if f is None:
            return {"ok": True, "total": 0, "items": []}
        where, vals = f
        order = _SORTS.get(sort or "", "id DESC")
        limit = max(1, min(limit, 200))
        total = conn.execute(
            f"SELECT COUNT(*) FROM item WHERE {' AND '.join(where)}", vals).fetchone()[0]
        rows = conn.execute(
            f"""SELECT i.*,
                   (SELECT p.file_path FROM photo p
                     WHERE p.item_id = i.id AND p.origin = 'archive'
                     ORDER BY CASE p.shot_type WHEN 'front' THEN 0
                              WHEN 'back' THEN 1 ELSE 2 END, p.id
                     LIMIT 1) AS thumb_path
            FROM item i WHERE {' AND '.join(where)}
            ORDER BY {order} LIMIT ? OFFSET ?""", [*vals, limit, offset]).fetchall()

    role = user["role"]
    items = []
    for r in rows:
        d = dict(r)
        tp = d.pop("thumb_path", None)
        tp = photos.ensure_thumb(tp) if tp else None   # 懒生成 480px 缩略图
        d["thumb"] = f"/media/{tp}" if tp else None
        items.append(_strip_financial(d, role))
    return {"ok": True, "total": total, "items": items}


@router.get("/items/by-code/{code}")
def item_by_code(code: str, _: dict = Depends(require_any)):
    """盘点扫码用：内部编号或评级编号精确定位。"""
    code = code.strip()
    with get_conn() as conn:
        row = conn.execute(
            """SELECT i.id, i.item_code, i.name, i.status, i.location_id,
                      l.name AS location_name
               FROM item i LEFT JOIN location l ON l.id = i.location_id
               WHERE i.item_code = ? OR i.cert_no = ? LIMIT 1""",
            (code, code)).fetchone()
    if not row:
        raise HTTPException(404, "未找到该编号")
    return dict(row)


@router.get("/items/{item_id}")
def item_detail(item_id: int, user: dict = Depends(require_any)):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM item WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "藏品不存在")
        photos = conn.execute(
            "SELECT id, shot_type, origin, source_url, fetched_at, file_path, exif_time "
            "FROM photo WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
        events = conn.execute(
            "SELECT kind, detail, operator, created_at FROM event "
            "WHERE item_id=? ORDER BY id DESC LIMIT 30", (item_id,)).fetchall()
        vhist = conn.execute(
            "SELECT old_value, new_value, source, operator, created_at FROM value_history "
            "WHERE item_id=? ORDER BY id DESC LIMIT 20", (item_id,)).fetchall()
        # 凭证附件：仅录入及以上可见（含价格信息，对只读用户隐藏）
        atts = []
        if user["role"] in ("admin", "staff"):
            atts = [dict(a, url=f"/media/{a['file_path']}".replace("\\", "/")) for a in
                    conn.execute("SELECT id, category, file_path, notes, created_at "
                                 "FROM attachment WHERE item_id=? ORDER BY id", (item_id,))]
    out = dict(row)
    out["photos"] = [dict(p, url=f"/media/{p['file_path']}".replace("\\", "/"))
                     for p in photos]
    out["events"] = [dict(e) for e in events]
    out["value_history"] = [dict(v) for v in vhist]
    out["attachments"] = atts
    if out.get("intro_audio"):
        out["intro_audio_url"] = f"/media/{out['intro_audio']}"
    return _strip_financial(out, user["role"])


@router.delete("/items/{item_id}")
def delete_item(item_id: int, _: dict = Depends(require_admin)):
    import shutil
    with get_conn() as conn:
        if not conn.execute("SELECT id FROM item WHERE id=?", (item_id,)).fetchone():
            raise HTTPException(404, "藏品不存在")
        models.fts_delete(conn, item_id)
        for t in ("photo", "recognition", "cert_fetch", "event"):
            conn.execute(f"DELETE FROM {t} WHERE item_id=?", (item_id,))
        conn.execute("DELETE FROM item WHERE id=?", (item_id,))
    shutil.rmtree(PHOTO_DIR / str(item_id), ignore_errors=True)
    return {"ok": True}


@router.delete("/photos/{photo_id}")
def delete_photo(photo_id: int, _: dict = Depends(require_staff)):
    with get_conn() as conn:
        row = conn.execute("SELECT file_path FROM photo WHERE id=?",
                           (photo_id,)).fetchone()
        if not row:
            raise HTTPException(404, "照片不存在")
        conn.execute("DELETE FROM photo WHERE id=?", (photo_id,))
    (PHOTO_DIR / row["file_path"]).unlink(missing_ok=True)
    return {"ok": True}


# ---------- 凭证附件（来源合法性留档：发票/协议/收据） ----------

ATT_CATS = ("发票", "收据", "转让协议", "证书", "其他")


@router.post("/items/{item_id}/attachments")
async def upload_attachment(item_id: int, file: UploadFile = File(...),
                            category: str = Form("其他"),
                            notes: str = Form(""),
                            user: dict = Depends(require_staff)):
    if category not in ATT_CATS:
        raise HTTPException(400, "凭证类型不对")
    data = await file.read()
    if not data or len(data) > UPLOAD_MAX:
        raise HTTPException(400, "空文件或文件太大（限16MB）")
    try:
        img = _PILImage.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise HTTPException(400, "请上传图片格式的凭证（拍照即可）")
    with get_conn() as conn:
        if not conn.execute("SELECT id FROM item WHERE id=?", (item_id,)).fetchone():
            raise HTTPException(404, "藏品不存在")
        d = PHOTO_DIR / str(item_id)
        d.mkdir(parents=True, exist_ok=True)
        fname = f"att_{_dt.now():%Y%m%d_%H%M%S_%f}.jpg"[:-4] + ".jpg"
        path = d / fname
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        if max(img.size) > 2000:
            img.thumbnail((2000, 2000))
        img.save(path, "JPEG", quality=85)
        rel = f"{item_id}/{fname}".replace("\\", "/")
        cur = conn.execute(
            "INSERT INTO attachment(item_id, category, file_path, notes, created_by) "
            "VALUES(?,?,?,?,?)",
            (item_id, category, rel, notes or None, user["username"]))
        conn.execute("INSERT INTO event(item_id, kind, detail, operator) VALUES(?,?,?,?)",
                     (item_id, "凭证上传", f"{category}×1", user["username"]))
        return {"ok": True, "id": cur.lastrowid, "url": f"/media/{rel}"}


@router.delete("/attachments/{att_id}")
def delete_attachment(att_id: int, user: dict = Depends(require_staff)):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT item_id, file_path, category FROM attachment WHERE id=?",
            (att_id,)).fetchone()
        if not row:
            raise HTTPException(404, "凭证不存在")
        conn.execute("DELETE FROM attachment WHERE id=?", (att_id,))
        conn.execute("INSERT INTO event(item_id, kind, detail, operator) VALUES(?,?,?,?)",
                     (row["item_id"], "凭证删除", row["category"], user["username"]))
    (PHOTO_DIR / row["file_path"]).unlink(missing_ok=True)
    return {"ok": True}


@router.get("/pending")
def pending_items(_: dict = Depends(require_any)):
    with get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM item WHERE status='draft'").fetchone()[0]
        rows = conn.execute(
            "SELECT id, name, cert_no, grade_raw, created_at FROM item "
            "WHERE status='draft' ORDER BY id DESC LIMIT 100").fetchall()
    return {"ok": True, "count": count, "items": [dict(r) for r in rows]}


@router.get("/needs-photos")
def needs_photos(_: dict = Depends(require_any)):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT i.id, i.item_code, i.name, i.status, i.grade_co, i.grade_raw,
                      COALESCE(SUM(p.shot_type='front'), 0) AS fronts,
                      COALESCE(SUM(p.shot_type='back'), 0)  AS backs
               FROM item i
               LEFT JOIN photo p ON p.item_id = i.id AND p.origin = 'archive'
               WHERE i.status IN ('draft', 'in_stock')
               GROUP BY i.id
               HAVING fronts = 0 OR backs = 0
               ORDER BY i.id DESC LIMIT 300""").fetchall()
    items = []
    for r in rows:
        missing = [t for t, v in (("front", r["fronts"]), ("back", r["backs"])) if not v]
        items.append({**dict(r), "missing": missing})
    return {"ok": True, "count": len(items), "items": items}


# ---------- 统计 ----------

@router.get("/stats")
def stats(user: dict = Depends(require_any)):
    with get_conn() as conn:
        def group(sql: str):
            return {r[0] or "未填": r[1] for r in conn.execute(sql).fetchall()}
        est_total = conn.execute("SELECT SUM(est_value) FROM item").fetchone()[0] or 0
        return {
            "ok": True,
            "total": conn.execute("SELECT COUNT(*) FROM item").fetchone()[0],
            "confirmed": conn.execute(
                "SELECT COUNT(*) FROM item WHERE status!='draft'").fetchone()[0],
            "draft": conn.execute(
                "SELECT COUNT(*) FROM item WHERE status='draft'").fetchone()[0],
            "photos": conn.execute("SELECT COUNT(*) FROM photo").fetchone()[0],
            "pub_views": conn.execute(
                "SELECT COALESCE(SUM(pub_views),0) FROM item").fetchone()[0],
            "attachments": conn.execute("SELECT COUNT(*) FROM attachment").fetchone()[0],
            "est_total": est_total if user["role"] == "admin" else 0,
            "by_category": group("SELECT category, COUNT(*) FROM item GROUP BY category"),
            "by_status": group("SELECT status, COUNT(*) FROM item GROUP BY status"),
            "by_series": group("SELECT series, COUNT(*) FROM item GROUP BY series"),
            "by_grade_co": group(
                "SELECT grade_co, COUNT(*) FROM item GROUP BY grade_co "
                "ORDER BY COUNT(*) DESC LIMIT 10"),
        }


# ---------- Excel 导出（含价格，仅管理员） ----------

@router.get("/export/xlsx")
def export_xlsx(request: Request, q: Optional[str] = None,
                category: Optional[str] = None, series: Optional[str] = None,
                note_type: Optional[str] = None, bank: Optional[str] = None,
                printer: Optional[str] = None, doc_type: Optional[str] = None,
                dynasty: Optional[str] = None, status: Optional[str] = None,
                level: Optional[str] = None, location_id: Optional[int] = None,
                year_from: Optional[int] = None, year_to: Optional[int] = None,
                sort: Optional[str] = None,
                _: dict = Depends(require_admin)):
    """按当前筛选条件导出（与检索页同一套筛选语义），不带参数=导全部。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "藏品台账"
    ws.append([c[1] for c in EXPORT_COLS])
    with get_conn() as conn:
        f = _item_filters(conn, q, category, series, note_type, bank, printer,
                          doc_type, dynasty, status, level, location_id,
                          year_from, year_to)
        if f is None:
            rows = []
        else:
            where, vals = f
            order = _SORTS.get(sort or "", "id DESC")
            rows = conn.execute(
                f"SELECT {', '.join(c[0] for c in EXPORT_COLS)} FROM item "
                f"WHERE {' AND '.join(where)} ORDER BY {order}", vals).fetchall()
    for r in rows:
        d = dict(r)
        d["status"] = STATUS_CN.get(d["status"], d["status"])
        ws.append([d[c[0]] for c in EXPORT_COLS])
    for i, w in enumerate((14, 30, 9, 12, 16, 11, 16, 10, 9, 12, 10, 14, 8,
                           14, 10, 12, 10, 18, 10, 8, 14, 8, 8, 10, 12, 10,
                           10, 12, 12, 10, 26, 20), start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    from datetime import datetime as _dt
    fname = quote(f"藏品台账_{_dt.now():%Y%m%d_%H%M}.xlsx")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=items.xlsx; filename*=UTF-8''{fname}"})


# ---------- 藏品总登记账（文物行业法定账目格式，备案年检用） ----------

@router.get("/export/register")
def export_register(_: dict = Depends(require_admin)):
    """总登记账：登记号/名称/年代/来源/现状/等级/入藏日期。
    逐件登记、按入藏先后排序——符合博物馆藏品总登记账通行格式。"""
    wb = Workbook()
    ws = wb.active
    ws.title = "藏品总登记账"
    ws.append(["登记号", "名称", "品类", "年代/纪年", "发行机构/来源", "来源方式",
               "藏品等级", "现状", "评级", "入藏日期", "备注"])
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT item_code, name, category, year_text, era,
                      bank, doc_type, source, level, status,
                      grade_co, grade_raw, purchase_date, created_at, notes
               FROM item WHERE status != 'draft' ORDER BY id ASC""").fetchall()
    for r in rows:
        grade = " ".join(str(x) for x in (r["grade_co"], r["grade_raw"]) if x)
        ws.append([
            r["item_code"], r["name"], r["category"],
            r["year_text"] or r["era"], r["bank"] or r["doc_type"],
            r["source"], r["level"],
            STATUS_CN.get(r["status"], r["status"]), grade,
            r["purchase_date"] or (r["created_at"] or "")[:10],
            r["notes"],
        ])
    for i, w in enumerate((16, 30, 10, 14, 16, 10, 12, 10, 14, 12, 26), start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = quote(f"藏品总登记账_{_dt.now():%Y%m%d_%H%M}.xlsx")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=register.xlsx; filename*=UTF-8''{fname}"})


# ---------- 库位（展柜→层→盒） ----------

class LocationPayload(BaseModel):
    name: str
    parent_id: Optional[int] = None
    code: Optional[str] = None


GRID_COLS = 8   # 场馆平面图每行格数


@router.get("/locations")
def list_locations(_: dict = Depends(require_any)):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT l.id, l.parent_id, l.name, l.code,
                      l.grid_x, l.grid_y, l.grid_w, l.grid_h,
                      (SELECT COUNT(*) FROM item i WHERE i.location_id = l.id) AS items
               FROM location l
               ORDER BY (l.parent_id IS NULL) DESC, l.parent_id, l.id""").fetchall()
    return [dict(r) for r in rows]


@router.post("/locations")
def add_location(p: LocationPayload, _: dict = Depends(require_admin)):
    if not p.name.strip():
        raise HTTPException(400, "名称不能为空")
    if p.parent_id is not None:
        with get_conn() as conn:
            if not conn.execute("SELECT id FROM location WHERE id=?",
                                (p.parent_id,)).fetchone():
                raise HTTPException(400, "上级库位不存在")
    with get_conn() as conn:
        code = (p.code or "").strip()
        gx = gy = None
        if p.parent_id is None:
            if not code:
                # 顶层展柜自动编号 C01/C02…（编号体系：平面格式图的格子号）
                n = 1
                while conn.execute("SELECT 1 FROM location WHERE code=?",
                                   (f"C{n:02d}",)).fetchone():
                    n += 1
                code = f"C{n:02d}"
            # 自动分配平面图第一个空格子
            taken = {(r["grid_x"], r["grid_y"]) for r in conn.execute(
                "SELECT grid_x, grid_y FROM location "
                "WHERE parent_id IS NULL AND grid_x IS NOT NULL")}
            y = 0
            while True:
                for x in range(GRID_COLS):
                    if (x, y) not in taken:
                        gx, gy = x, y
                        break
                if gx is not None:
                    break
                y += 1
        cur = conn.execute(
            "INSERT INTO location(parent_id, name, code, grid_x, grid_y) VALUES(?,?,?,?,?)",
            (p.parent_id, p.name.strip(), code or None, gx, gy))
        return {"ok": True, "id": cur.lastrowid, "code": code or None}


class PositionPayload(BaseModel):
    x: int
    y: int
    w: Optional[int] = None    # 占几格宽（横柜=2）
    h: Optional[int] = None    # 占几格高（竖柜=2）


def _occupied(conn, exclude_id: int) -> set:
    """所有其他顶层柜占用的格子集合（展开跨距）。"""
    cells = set()
    for r in conn.execute(
            "SELECT id, grid_x, grid_y, grid_w, grid_h FROM location "
            "WHERE parent_id IS NULL AND grid_x IS NOT NULL AND id != ?",
            (exclude_id,)).fetchall():
        w = max(1, r["grid_w"] or 1)
        h = max(1, r["grid_h"] or 1)
        for dx in range(w):
            for dy in range(h):
                cells.add((r["grid_x"] + dx, r["grid_y"] + dy))
    return cells


@router.put("/locations/{loc_id}/position")
def move_location(loc_id: int, p: PositionPayload, _: dict = Depends(require_admin)):
    """平面图摆放：移动柜子到 (x,y)，可同时调整跨距 w/h（横柜 2x1、竖柜 1x2）。
    与其他柜子的占格重叠时拒绝（400），回字形即多段柜子拼摆。"""
    if not (0 <= p.x < GRID_COLS and 0 <= p.y < 200):
        raise HTTPException(400, "格子坐标超出平面范围")
    with get_conn() as conn:
        me = conn.execute(
            "SELECT id, grid_x, grid_y, grid_w, grid_h FROM location "
            "WHERE id=? AND parent_id IS NULL", (loc_id,)).fetchone()
        if not me:
            raise HTTPException(404, "展柜不存在")
        w = max(1, min(6, p.w if p.w is not None else (me["grid_w"] or 1)))
        h = max(1, min(20, p.h if p.h is not None else (me["grid_h"] or 1)))
        taken = _occupied(conn, loc_id)
        for dx in range(w):
            for dy in range(h):
                if (p.x + dx, p.y + dy) in taken:
                    raise HTTPException(400, "与相邻展柜位置重叠，请先挪开或缩小尺寸")
        conn.execute(
            "UPDATE location SET grid_x=?, grid_y=?, grid_w=?, grid_h=? WHERE id=?",
            (p.x, p.y, w, h, loc_id))
    return {"ok": True}


@router.put("/locations/{loc_id}")
def rename_location(loc_id: int, p: LocationPayload, _: dict = Depends(require_admin)):
    if not p.name.strip():
        raise HTTPException(400, "名称不能为空")
    if p.parent_id == loc_id:
        raise HTTPException(400, "不能把自己设为上级")
    with get_conn() as conn:
        # 防环：新上级不能是自己的后代（否则树会成死循环）
        walk = p.parent_id
        while walk is not None:
            if walk == loc_id:
                raise HTTPException(400, "不能把库位移到它自己的下级下面")
            row = conn.execute("SELECT parent_id FROM location WHERE id=?",
                               (walk,)).fetchone()
            walk = row["parent_id"] if row else None
        cur = conn.execute("UPDATE location SET name=?, parent_id=? WHERE id=?",
                           (p.name.strip(), p.parent_id, loc_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "库位不存在")
    return {"ok": True}


@router.delete("/locations/{loc_id}")
def delete_location(loc_id: int, _: dict = Depends(require_admin)):
    with get_conn() as conn:
        if conn.execute("SELECT id FROM location WHERE parent_id=?",
                        (loc_id,)).fetchone():
            raise HTTPException(400, "先删除该库位下的子库位")
        n = conn.execute("SELECT COUNT(*) FROM item WHERE location_id=?",
                         (loc_id,)).fetchone()[0]
        if n:
            raise HTTPException(400, f"该库位还有 {n} 件藏品，请先移走")
        cur = conn.execute("DELETE FROM location WHERE id=?", (loc_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "库位不存在")
    return {"ok": True}


# ---------- Excel 导入（仅管理员，与导出同格式可回灌） ----------

@router.post("/import/xlsx")
async def import_xlsx(file: UploadFile = File(...), user: dict = Depends(require_admin)):
    if file.size and file.size > UPLOAD_MAX:
        raise HTTPException(413, "文件太大（限 16MB）")
    data = await file.read()
    if not data:
        raise HTTPException(400, "空文件")
    cn2field = {cn: f for f, cn in EXPORT_COLS}
    rev_status = {v: k for k, v in STATUS_CN.items()}

    def parse_val(f, v):
        if v is None or v == "":
            return None
        if f in ("purchase_price", "est_value"):
            try:
                return float(v)
            except (TypeError, ValueError):
                return None
        if f == "year_sort":
            try:
                return int(v)
            except (TypeError, ValueError):
                return None
        return str(v)

    imported = skipped = errors = 0
    wb = load_workbook(io.BytesIO(data), read_only=True)
    ws = wb.active
    it = ws.iter_rows(values_only=True)
    header = next(it, None)
    if not header:
        raise HTTPException(400, "表格为空")
    hmap = {str(h).strip(): i for i, h in enumerate(header) if h}

    def cell(row, cn):
        i = hmap.get(cn)
        if i is None or i >= len(row):
            return None
        return row[i]

    with get_conn() as conn:
        for row in it:
            if row is None or all(v in (None, "") for v in row):
                continue
            try:
                cert_no = cell(row, "评级编号")
                if cert_no:
                    cert_no = str(cert_no).strip()
                    if conn.execute("SELECT 1 FROM item WHERE cert_no=?",
                                    (cert_no,)).fetchone():
                        skipped += 1
                        continue
                code = cell(row, "内部编号")
                code = str(code).strip() if code else None
                if code and conn.execute("SELECT 1 FROM item WHERE item_code=?",
                                         (code,)).fetchone():
                    skipped += 1
                    continue
                fields = {f: parse_val(f, cell(row, cn))
                          for f, cn in EXPORT_COLS if f not in ("status", "item_code")}
                name = fields.get("name")
                if not name:
                    parts = [fields.get("bank"), fields.get("note_type"),
                             fields.get("denomination"), fields.get("era")]
                    name = " ".join(str(p) for p in parts if p) or "未命名藏品"
                status_raw = cell(row, "状态")
                status = rev_status.get(str(status_raw).strip(), "in_stock") \
                    if status_raw else "in_stock"
                cur = conn.execute(
                    f"""INSERT INTO item(status, item_code, {', '.join(fields.keys())})
                        VALUES(?, ?, {', '.join('?' * len(fields))})""",
                    [status, code] + list(fields.values()))
                item_id = cur.lastrowid
                if not code:
                    conn.execute("UPDATE item SET item_code=? WHERE id=?",
                                 (f"M-{_dt.now().year}-{item_id:04d}", item_id))
                models.fts_sync(conn, item_id, fields)
                conn.execute(
                    "INSERT INTO event(item_id, kind, detail, operator) VALUES(?,?,?,?)",
                    (item_id, "入库", "Excel 导入", user["username"]))
                imported += 1
            except Exception:
                errors += 1
    return {"ok": True, "imported": imported, "skipped": skipped, "errors": errors}


# ---------- 数据库备份（仅管理员，SQLite backup API 保证一致性） ----------

@router.get("/backup/db")
def backup_db(_: dict = Depends(require_admin)):
    src = get_conn()
    fd, tmp = _tempfile.mkstemp(suffix=".db")
    os.close(fd)
    dest = None
    try:
        dest = _sqlite3.connect(tmp)
        src.backup(dest)
    finally:
        src.close()
        if dest is not None:
            dest.close()   # 不关的话 Windows 上文件被锁，删不掉
    fname = quote(f"藏品数据库_{_dt.now():%Y%m%d_%H%M}.db")
    return FileResponse(
        tmp, filename="museum.db",
        headers={"Content-Disposition": f"attachment; filename=museum.db; filename*=UTF-8''{fname}"},
        background=BackgroundTask(os.unlink, tmp))


# ---------- 批量识别队列（照片扔进去人就能走） ----------

@router.post("/batch")
async def upload_batch(files: List[UploadFile] = File(...),
                       user: dict = Depends(require_staff)):
    files = files[:60]   # 一次最多 60 张，多了分几次传
    with get_conn() as conn:
        cur = conn.execute("INSERT INTO batch_job(created_by) VALUES(?)",
                           (user["username"],))
        job_id = cur.lastrowid
    queued = await _save_batch_files(job_id, files)
    return {"ok": True, "job_id": job_id, "queued": queued}


@router.post("/batch/{job_id}/files")
async def append_batch(job_id: int, files: List[UploadFile] = File(...),
                       _: dict = Depends(require_staff)):
    with get_conn() as conn:
        if not conn.execute("SELECT id FROM batch_job WHERE id=?", (job_id,)).fetchone():
            raise HTTPException(404, "任务不存在")
        conn.execute("UPDATE batch_job SET status='running' WHERE id=?", (job_id,))
    queued = await _save_batch_files(job_id, files[:60])
    return {"ok": True, "job_id": job_id, "queued": queued}


async def _save_batch_files(job_id: int, files: List[UploadFile]) -> int:
    d = BATCH_DIR / str(job_id)
    d.mkdir(parents=True, exist_ok=True)
    queued = 0
    total_bytes = 0
    with get_conn() as conn:
        for i, f in enumerate(files):
            try:
                if f.size and f.size > UPLOAD_MAX:
                    continue
                data = await f.read()
                if not data or len(data) > UPLOAD_MAX:
                    continue
                if total_bytes + len(data) > MAX_BATCH_BYTES:
                    break
                total_bytes += len(data)
                path = d / f"{_dt.now():%H%M%S}_{i:03d}.jpg"
                path.write_bytes(data)
                conn.execute(
                    "INSERT INTO batch_item(job_id, filename, path) VALUES(?,?,?)",
                    (job_id, f.filename, str(path)))
                conn.execute("UPDATE batch_job SET total = total + 1 WHERE id=?",
                             (job_id,))
                queued += 1
            except Exception:
                continue
    return queued


@router.get("/batch")
def batch_list(_: dict = Depends(require_staff)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM batch_job ORDER BY id DESC LIMIT 20").fetchall()
    return [dict(r) for r in rows]


@router.get("/batch/{job_id}")
def batch_detail(job_id: int, _: dict = Depends(require_staff)):
    with get_conn() as conn:
        job = conn.execute("SELECT * FROM batch_job WHERE id=?", (job_id,)).fetchone()
        if not job:
            raise HTTPException(404, "任务不存在")
        items = conn.execute(
            "SELECT id, filename, status, item_id, message FROM batch_item "
            "WHERE job_id=? ORDER BY id LIMIT 500", (job_id,)).fetchall()
    return {"ok": True, "job": dict(job), "items": [dict(i) for i in items]}


@router.post("/batch/{job_id}/stop")
def batch_stop(job_id: int, _: dict = Depends(require_admin)):
    with get_conn() as conn:
        conn.execute("UPDATE batch_job SET status='done' WHERE id=?", (job_id,))
    return {"ok": True}


# ---------- 站点设置（标题/LOGO 自定义，仅管理员） ----------

class SettingsPayload(BaseModel):
    site_name: str = ""
    logo_char: str = ""


@router.get("/settings")
def get_settings_ep(_: dict = Depends(require_admin)):
    return {"ok": True,
            "site_name": get_setting("site_name", "钱币博物馆"),
            "logo_char": get_setting("logo_char", "泉"),
            "logo_img": (PHOTO_DIR / "settings" / "logo.png").exists()}


@router.post("/settings")
def set_settings_ep(p: SettingsPayload, _: dict = Depends(require_admin)):
    if p.site_name.strip():
        set_setting("site_name", p.site_name.strip()[:30])
    if p.logo_char.strip():
        set_setting("logo_char", p.logo_char.strip()[:2])
    return {"ok": True}


@router.post("/settings/logo")
async def upload_logo(file: UploadFile = File(...), _: dict = Depends(require_admin)):
    data = await file.read()
    if not data or (file.size and file.size > UPLOAD_MAX):
        raise HTTPException(400, "空文件或图片太大")
    try:
        img = _PILImage.open(io.BytesIO(data))
        img.load()
    except Exception:
        raise HTTPException(400, "图片无法识别")
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA")
    if max(img.size) > 512:
        img.thumbnail((512, 512))
    d = PHOTO_DIR / "settings"
    d.mkdir(parents=True, exist_ok=True)
    img.save(d / "logo.png", "PNG")
    return {"ok": True}


@router.delete("/settings/logo")
def delete_logo(_: dict = Depends(require_admin)):
    (PHOTO_DIR / "settings" / "logo.png").unlink(missing_ok=True)
    return {"ok": True}


# ---------- 讲解介绍：AI/模板生成 + TTS 30秒语音（展签二维码即扫即听） ----------

VOICES = {"xiaoxiao": "zh-CN-XiaoxiaoNeural",
          "yunjian": "zh-CN-YunjianNeural",
          "yunxi": "zh-CN-YunxiNeural"}


def _intro_template(d: dict) -> str:
    """无大模型插口时的模板兜底：由档案字段拼一段口语化介绍。"""
    parts = []
    if d.get("name"):
        parts.append(f"这件藏品是{d['name']}")
    era = d.get("year_text") or d.get("era")
    if era:
        parts.append(f"铸造发行于{era}")
    if d.get("bank"):
        parts.append(f"由{d['bank']}发行")
    if d.get("printer"):
        parts.append(f"{d['printer']}印制")
    if d.get("denomination"):
        parts.append(f"面值{d['denomination']}")
    if d.get("material"):
        parts.append(f"{d['material']}质")
    s = "，".join(parts)
    if s:
        s += "。"
    if d.get("variety"):
        s += f"版别特征：{d['variety']}。"
    if d.get("level"):
        s += f"本馆定级为{d['level']}。"
    return s[:200]


@router.post("/items/{item_id}/intro/generate")
def intro_generate(item_id: int, user: dict = Depends(require_staff)):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM item WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "藏品不存在")
        d = dict(row)
    summary = "；".join(f"{k}：{v}" for k, v in {
        "名称": d.get("name"), "品类": d.get("category"), "系列": d.get("series"),
        "机构": d.get("bank"), "券种": d.get("note_type"), "年号": d.get("era"),
        "面值": d.get("denomination"), "纪年": d.get("year_text"),
        "印刷厂": d.get("printer"), "材质": d.get("material"), "版别": d.get("variety"),
        "评级": " ".join(str(x) for x in (d.get("grade_co"), d.get("grade_raw")) if x),
        "备注": d.get("notes"),
    }.items() if v)
    if services_llm.is_enabled():
        prompt = ("你是钱币博物馆的讲解员。根据下面的藏品档案，写一段面向普通观众的口语化介绍，"
                  "约120字（朗读约30秒），讲来历和看点，不要罗列字段、不要客套的开头和结尾：\n"
                  + summary)
        text = services_llm.chat(prompt, None, 400).strip().strip('"')
        source = "AI生成"
    else:
        text = _intro_template(d)
        source = "模板生成"
    text = text[:220]
    with get_conn() as conn:
        conn.execute("UPDATE item SET intro_text=? WHERE id=?", (text, item_id))
        conn.execute(
            "INSERT INTO event(item_id, kind, detail, operator) VALUES(?,?,?,?)",
            (item_id, "介绍生成", f"{source}，{len(text)}字", user["username"]))
        models.fts_sync(conn, item_id, {
            **{k: row[k] for k in ("name", "notes", "region", "variety", "era",
                                   "denomination", "cert_no", "item_code")},
            "intro_text": text})
    return {"ok": True, "text": text, "source": source}


@router.post("/items/{item_id}/intro/tts")
async def intro_tts(item_id: int, payload: Optional[dict] = Body(default={}),
                    user: dict = Depends(require_staff)):
    with get_conn() as conn:
        row = conn.execute("SELECT intro_text FROM item WHERE id=?",
                           (item_id,)).fetchone()
    if not row:
        raise HTTPException(404, "藏品不存在")
    text = (row["intro_text"] or "").strip()
    if not text:
        raise HTTPException(400, "请先填写或生成介绍文案")
    text = text[:240]   # 约30秒朗读
    voice = VOICES.get((payload or {}).get("voice", "xiaoxiao"), VOICES["xiaoxiao"])
    import edge_tts
    d = PHOTO_DIR / str(item_id)
    d.mkdir(parents=True, exist_ok=True)
    out = d / "intro.mp3"
    try:
        await edge_tts.Communicate(text, voice).save(str(out))
    except Exception as e:
        raise HTTPException(502, f"语音合成失败（需可访问微软TTS服务）：{str(e)[:120]}")
    rel = f"{item_id}/intro.mp3"
    with get_conn() as conn:
        conn.execute("UPDATE item SET intro_audio=? WHERE id=?", (rel, item_id))
    return {"ok": True, "url": f"/media/{rel}"}


@router.post("/items/{item_id}/estimate")
def estimate_ep(item_id: int, user: dict = Depends(require_staff)):
    """AI 参考估值：基于档案生成参考区间。明确标注为参考，非市场成交价。"""
    if not services_llm.is_enabled():
        raise HTTPException(400, "AI参考估值需要配置大模型插口（compose 环境变量）")
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM item WHERE id=?", (item_id,)).fetchone()
    if not row:
        raise HTTPException(404, "藏品不存在")
    d = dict(row)
    summary = "；".join(f"{k}：{v}" for k, v in {
        "名称": d.get("name"), "品类": d.get("category"), "系列": d.get("series"),
        "机构": d.get("bank"), "券种": d.get("note_type"), "纪年": d.get("year_text"),
        "面值": d.get("denomination"), "评级": " ".join(
            str(x) for x in (d.get("grade_co"), d.get("grade_raw")) if x),
        "版别": d.get("variety"), "等级": d.get("level"),
    }.items() if v)
    import json as _json
    import re as _re
    prompt = ("你是资深钱币评估师。根据藏品档案给出当前市场参考估值区间（人民币元），"
              '只输出JSON：{"low": 数字, "high": 数字, "reason": "不超过50字的理由"}。'
              "依据不足时放宽区间并说明：\n" + summary)
    raw = services_llm.chat(prompt, None, 300)
    m = _re.search(r"\{.*\}", raw, _re.S)
    if not m:
        raise HTTPException(502, "AI未返回可解析结果，请重试")
    try:
        data = _json.loads(m.group(0))
    except Exception:
        raise HTTPException(502, "AI返回内容无法解析，请重试")
    return {"ok": True, "low": data.get("low"), "high": data.get("high"),
            "reason": data.get("reason"), "note": "AI参考估值，非市场成交价"}


# ---------- 二维码生成（展签用） ----------

@router.get("/qr")
def qr_image(text: str, _: dict = Depends(require_any)):
    import qrcode
    img = qrcode.make(text)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


# ---------- 游客导览数据（免登录，只含展示字段，绝无价格/库位/来源） ----------

PUB_FIELDS = ("item_code", "name", "category", "series", "dynasty", "bank",
              "note_type", "printer", "doc_type", "era", "denomination",
              "variety", "material", "year_text", "region",
              "grade_co", "grade_raw", "cert_no",
              "level", "catalog_ref", "intro_text")


@router.get("/pub/item/{code}")
def pub_item(code: str, request: Request):
    if PUB_TOKEN and request.query_params.get("t") != PUB_TOKEN:
        # 配置了导览口令（PUB_TOKEN）时，必须带 ?t=口令 才能访问
        raise HTTPException(404, "未找到该藏品")
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM item WHERE item_code=?",
                           (code.strip(),)).fetchone()
        if not row:
            raise HTTPException(404, "未找到该藏品")
        conn.execute("UPDATE item SET pub_views = COALESCE(pub_views,0) + 1 "
                     "WHERE id=?", (row["id"],))   # 浏览计数（运营数据）
        photos = conn.execute(
            "SELECT file_path FROM photo WHERE item_id=? ORDER BY id", (row["id"],)
        ).fetchall()
    out = {k: row[k] for k in PUB_FIELDS if row[k]}
    out["pub_views"] = row["pub_views"]
    out["photos"] = [f"/media/{p['file_path']}".replace("\\", "/") for p in photos]
    return out


# ---------- 档案体检（数据质量报告，仅管理员） ----------

@router.get("/health-report")
def health_report(_: dict = Depends(require_admin)):
    with get_conn() as conn:
        one = lambda sql: conn.execute(sql).fetchone()[0]
        bdir = DATA_DIR / "backups"
        backups = sorted(bdir.glob("museum_*.db")) if bdir.exists() else []
        return {
            "ok": True,
            "no_location": one(
                "SELECT COUNT(*) FROM item WHERE location_id IS NULL "
                "AND status IN ('draft','in_stock')"),
            "no_front": one(
                "SELECT COUNT(*) FROM item i WHERE i.status IN ('draft','in_stock') "
                "AND NOT EXISTS(SELECT 1 FROM photo p WHERE p.item_id=i.id "
                "AND p.shot_type='front' AND p.origin='archive')"),
            "no_back": one(
                "SELECT COUNT(*) FROM item i WHERE i.status IN ('draft','in_stock') "
                "AND NOT EXISTS(SELECT 1 FROM photo p WHERE p.item_id=i.id "
                "AND p.shot_type='back' AND p.origin='archive')"),
            "no_name": one(
                "SELECT COUNT(*) FROM item WHERE name IS NULL OR name=''"),
            "no_year": one(
                "SELECT COUNT(*) FROM item WHERE year_text IS NULL OR year_text=''"),
            "no_grade": one(
                "SELECT COUNT(*) FROM item WHERE grade_co IS NULL OR grade_co IN ('','无')"),
            "dup_photos": one(
                "SELECT COUNT(*) FROM (SELECT phash FROM photo WHERE phash IS NOT NULL "
                "GROUP BY phash HAVING COUNT(*) > 1)"),
            "db_backups": len(backups),
            "db_backup_last": backups[-1].name if backups else None,
        }


# ---------- 全局操作日志（仅管理员） ----------

@router.get("/events")
def all_events(_: dict = Depends(require_admin)):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT e.id, e.kind, e.detail, e.operator, e.created_at,
                      i.item_code, i.name
               FROM event e LEFT JOIN item i ON i.id = e.item_id
               ORDER BY e.id DESC LIMIT 200""").fetchall()
    return [dict(r) for r in rows]
