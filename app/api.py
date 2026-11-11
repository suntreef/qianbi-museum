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
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from openpyxl import Workbook, load_workbook
from openpyxl.utils import get_column_letter
from pydantic import BaseModel
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import models
from .auth import COOKIE, hash_password, login_user, make_token, require_role
from .config import PHOTO_DIR
from .ingest import attach_photo, confirm_item, create_manual, ingest_label
from .models import get_conn

router = APIRouter(prefix="/api")

require_any = require_role()                    # 任何已登录用户
require_staff = require_role("admin", "staff")  # 录入及以上
require_admin = require_role("admin")           # 仅管理员

STATUS_CN = {"draft": "草稿", "in_stock": "在库", "reviewing": "送评中",
             "exhibited": "展出中", "lent": "借出", "consigned": "寄售",
             "sold": "已售", "lost": "丢失"}

EXPORT_COLS = [
    ("item_code", "内部编号"), ("name", "名称"), ("category", "品类"),
    ("series", "系列"), ("bank", "发行机构"), ("note_type", "券种/类型"),
    ("printer", "印刷厂"), ("doc_type", "文书类型"), ("dynasty", "朝代"),
    ("era", "年号/品种"), ("denomination", "面值"), ("variety", "版别"),
    ("material", "材质"), ("year_text", "纪年"), ("year_sort", "公历年"),
    ("region", "地域"), ("grade_co", "评级公司"), ("cert_no", "评级编号"),
    ("grade_raw", "分数"), ("grade_scale", "分数制"), ("serial_no", "冠字号"),
    ("status", "状态"), ("source", "来源"), ("purchase_price", "采购价"),
    ("purchase_date", "采购日期"), ("est_value", "估值"), ("est_date", "估值日期"),
    ("notes", "备注"), ("created_at", "录入时间"),
]

FINANCIAL = ("purchase_price", "est_value")
UPLOAD_MAX = 16 * 1024 * 1024


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
                    samesite="lax", max_age=30 * 86400)
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
              "printer", "doc_type", "grade_co", "material", "cert_domain")


class DictPayload(BaseModel):
    type: str
    name: str
    aliases: Optional[str] = None
    sort: int = 50


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
            """INSERT INTO dict_entry(type, name, aliases, sort) VALUES(?,?,?,?)
               ON CONFLICT(type, name) DO UPDATE
               SET aliases=excluded.aliases, sort=excluded.sort""",
            (p.type, p.name.strip(), p.aliases, p.sort))
    return {"ok": True}


@router.put("/dict/{entry_id}")
def update_dict(entry_id: int, p: DictPayload, _: dict = Depends(require_admin)):
    if p.type not in DICT_TYPES or not p.name.strip():
        raise HTTPException(400, "参数不对")
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE dict_entry SET type=?, name=?, aliases=?, sort=? WHERE id=?",
            (p.type, p.name.strip(), p.aliases, p.sort, entry_id))
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
                 location_id: Optional[int] = None,
                 year_from: Optional[int] = None, year_to: Optional[int] = None,
                 limit: int = 50, offset: int = 0,
                 user: dict = Depends(require_any)):
    where, vals = ["1=1"], []
    with get_conn() as conn:
        if q:
            # 中文全文索引优先（≥3 字子串，走 trigram 索引，大库也快）；
            # 不足 3 字或库不支持 FTS 时降级 LIKE
            ids = models.fts_search_ids(conn, q)
            if ids is not None:
                if not ids:
                    return {"ok": True, "total": 0, "items": []}
                where.append(f"id IN ({','.join('?' * len(ids))})")
                vals.extend(ids)
            else:
                like = f"%{q}%"
                cols = ("name", "era", "cert_no", "item_code", "region", "variety",
                        "notes", "denomination", "serial_no", "bank", "note_type")
                where.append("(" + " OR ".join(f"{c} LIKE ?" for c in cols) + ")")
                vals.extend([like] * len(cols))
        for col, v in (("category", category), ("series", series),
                       ("note_type", note_type), ("bank", bank),
                       ("printer", printer), ("doc_type", doc_type),
                       ("dynasty", dynasty), ("status", status)):
            if v:
                where.append(f"{col}=?")
                vals.append(v)
        if year_from is not None:
            where.append("year_sort >= ?")
            vals.append(year_from)
        if year_to is not None:
            where.append("year_sort <= ?")
            vals.append(year_to)
        if location_id is not None:
            if location_id == -1:      # “未入库位”
                where.append("location_id IS NULL")
            else:
                where.append("location_id = ?")
                vals.append(location_id)

        limit = max(1, min(limit, 200))
        total = conn.execute(
            f"SELECT COUNT(*) FROM item WHERE {' AND '.join(where)}", vals).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM item WHERE {' AND '.join(where)} "
            "ORDER BY id DESC LIMIT ? OFFSET ?", [*vals, limit, offset]).fetchall()

    role = user["role"]
    return {"ok": True, "total": total,
            "items": [_strip_financial(dict(r), role) for r in rows]}


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
    out = dict(row)
    out["photos"] = [dict(p, url=f"/media/{p['file_path']}") for p in photos]
    out["events"] = [dict(e) for e in events]
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
def export_xlsx(_: dict = Depends(require_admin)):
    wb = Workbook()
    ws = wb.active
    ws.title = "藏品台账"
    ws.append([c[1] for c in EXPORT_COLS])
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT {', '.join(c[0] for c in EXPORT_COLS)} FROM item ORDER BY id"
        ).fetchall()
    for r in rows:
        d = dict(r)
        d["status"] = STATUS_CN.get(d["status"], d["status"])
        ws.append([d[c[0]] for c in EXPORT_COLS])
    for i, w in enumerate((14, 30, 9, 12, 16, 11, 16, 10, 9, 12, 10, 14, 8,
                           14, 10, 12, 10, 18, 10, 8, 14, 8, 8, 10, 12, 10,
                           10, 12, 26, 20), start=1):
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


# ---------- 库位（展柜→层→盒） ----------

class LocationPayload(BaseModel):
    name: str
    parent_id: Optional[int] = None
    code: Optional[str] = None


@router.get("/locations")
def list_locations(_: dict = Depends(require_any)):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT l.id, l.parent_id, l.name, l.code,
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
        cur = conn.execute(
            "INSERT INTO location(parent_id, name, code) VALUES(?,?,?)",
            (p.parent_id, p.name.strip(), (p.code or "").strip() or None))
        return {"ok": True, "id": cur.lastrowid}


@router.put("/locations/{loc_id}")
def rename_location(loc_id: int, p: LocationPayload, _: dict = Depends(require_admin)):
    if not p.name.strip():
        raise HTTPException(400, "名称不能为空")
    if p.parent_id == loc_id:
        raise HTTPException(400, "不能把自己设为上级")
    with get_conn() as conn:
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
