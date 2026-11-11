"""JSON API：录入、照片、确认、检索、字典、统计、导出。"""
import io
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .auth import COOKIE, make_token, require_auth
from .config import PHOTO_DIR
from .ingest import attach_photo, confirm_item, create_manual, ingest_label
from .models import get_conn

router = APIRouter(prefix="/api")

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


# ---------- 登录 ----------

@router.post("/logout")
def logout():
    resp = _json({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


# ---------- 字典 ----------

@router.get("/dict/{dict_type}")
def get_dict(dict_type: str, _: None = Depends(require_auth)):
    if dict_type not in ("category", "dynasty", "bank", "note_type", "series",
                         "printer", "doc_type", "grade_co", "material", "cert_domain"):
        raise HTTPException(404, "未知字典类型")
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT name, aliases FROM dict_entry WHERE type=? ORDER BY sort, id",
            (dict_type,)).fetchall()
    return [{"name": r["name"], "aliases": r["aliases"]} for r in rows]


# ---------- 采集（识别流水线） ----------

@router.post("/ingest/label")
async def ingest_label_ep(photo: UploadFile = File(...), _: None = Depends(require_auth)):
    data = await photo.read()
    if not data:
        raise HTTPException(400, "空文件")
    return await run_in_threadpool(ingest_label, data)


@router.post("/items/{item_id}/photos")
async def upload_photo(item_id: int, photo: UploadFile = File(...),
                       shot_type: str = Form("front"), _: None = Depends(require_auth)):
    data = await photo.read()
    if not data:
        raise HTTPException(400, "空文件")
    return await run_in_threadpool(attach_photo, item_id, data, shot_type)


# ---------- 藏品 ----------

class ItemPayload(BaseModel):
    fields: dict = {}
    confirm: bool = False
    operator: Optional[str] = None
    status: Optional[str] = None


@router.post("/items")
async def create_item_ep(payload: ItemPayload, _: None = Depends(require_auth)):
    item_id = await run_in_threadpool(create_manual, payload.fields)
    return {"ok": True, "item_id": item_id}


@router.post("/items/{item_id}")
async def update_item_ep(item_id: int, payload: ItemPayload,
                         _: None = Depends(require_auth)):
    return await run_in_threadpool(
        confirm_item, item_id, payload.fields, payload.confirm,
        payload.operator, payload.status)


@router.get("/items")
def search_items(q: Optional[str] = None, category: Optional[str] = None,
                 series: Optional[str] = None, note_type: Optional[str] = None,
                 bank: Optional[str] = None, printer: Optional[str] = None,
                 doc_type: Optional[str] = None, dynasty: Optional[str] = None,
                 status: Optional[str] = None, year_from: Optional[int] = None,
                 year_to: Optional[int] = None, limit: int = 50, offset: int = 0,
                 _: None = Depends(require_auth)):
    where, vals = ["1=1"], []

    def add(cond: str, *v):
        where.append(cond)
        vals.extend(v)

    if q:
        like = f"%{q}%"
        cols = ("name", "era", "cert_no", "item_code", "region", "variety",
                "notes", "denomination", "serial_no", "bank", "note_type")
        add("(" + " OR ".join(f"{c} LIKE ?" for c in cols) + ")", *([like] * len(cols)))
    for col, v in (("category", category), ("series", series),
                   ("note_type", note_type), ("bank", bank), ("printer", printer),
                   ("doc_type", doc_type), ("dynasty", dynasty), ("status", status)):
        if v:
            add(f"{col}=?", v)
    if year_from is not None:
        add("year_sort >= ?", year_from)
    if year_to is not None:
        add("year_sort <= ?", year_to)

    limit = max(1, min(limit, 200))
    with get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM item WHERE {' AND '.join(where)}", vals
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM item WHERE {' AND '.join(where)} "
            "ORDER BY id DESC LIMIT ? OFFSET ?", [*vals, limit, offset]).fetchall()
    return {"ok": True, "total": total, "items": [dict(r) for r in rows]}


@router.get("/items/{item_id}")
def item_detail(item_id: int, _: None = Depends(require_auth)):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM item WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise HTTPException(404, "藏品不存在")
        photos = conn.execute(
            "SELECT id, shot_type, origin, source_url, fetched_at, file_path, exif_time "
            "FROM photo WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
    out = dict(row)
    out["photos"] = [dict(p, url=f"/media/{p['file_path']}") for p in photos]
    return out


@router.get("/pending")
def pending_items(_: None = Depends(require_auth)):
    """待确认草稿队列。"""
    with get_conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM item WHERE status='draft'").fetchone()[0]
        rows = conn.execute(
            "SELECT id, name, cert_no, grade_raw, created_at FROM item "
            "WHERE status='draft' ORDER BY id DESC LIMIT 100").fetchall()
    return {"ok": True, "count": count, "items": [dict(r) for r in rows]}


# ---------- 工具 ----------

def _json(data: dict):
    from fastapi.responses import JSONResponse
    return JSONResponse(data)


@router.post("/login")
async def login(password: str = Form(...)):
    from .config import APP_PASSWORD
    if password != APP_PASSWORD:
        raise HTTPException(401, "口令不对")
    resp = _json({"ok": True})
    resp.set_cookie(COOKIE, make_token(), httponly=True, samesite="lax",
                    max_age=30 * 86400)
    return resp


# ---------- 照片 / 藏品删除 ----------

@router.delete("/photos/{photo_id}")
def delete_photo(photo_id: int, _: None = Depends(require_auth)):
    with get_conn() as conn:
        row = conn.execute("SELECT file_path FROM photo WHERE id=?",
                           (photo_id,)).fetchone()
        if not row:
            raise HTTPException(404, "照片不存在")
        conn.execute("DELETE FROM photo WHERE id=?", (photo_id,))
    (PHOTO_DIR / row["file_path"]).unlink(missing_ok=True)
    return {"ok": True}


@router.delete("/items/{item_id}")
def delete_item(item_id: int, _: None = Depends(require_auth)):
    import shutil
    with get_conn() as conn:
        if not conn.execute("SELECT id FROM item WHERE id=?", (item_id,)).fetchone():
            raise HTTPException(404, "藏品不存在")
        for t in ("photo", "recognition", "cert_fetch", "event"):
            conn.execute(f"DELETE FROM {t} WHERE item_id=?", (item_id,))
        conn.execute("DELETE FROM item WHERE id=?", (item_id,))
    shutil.rmtree(PHOTO_DIR / str(item_id), ignore_errors=True)
    return {"ok": True}


# ---------- 补拍队列 ----------

@router.get("/needs-photos")
def needs_photos(_: None = Depends(require_auth)):
    """缺正面或背面档案图的藏品清单。"""
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


# ---------- 字典管理 ----------

DICT_TYPES = ("category", "dynasty", "bank", "note_type", "series",
              "printer", "doc_type", "grade_co", "material", "cert_domain")


class DictPayload(BaseModel):
    type: str
    name: str
    aliases: Optional[str] = None
    sort: int = 50


@router.get("/dict-all")
def dict_all(_: None = Depends(require_auth)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM dict_entry ORDER BY type, sort, id").fetchall()
    return [dict(r) for r in rows]


@router.post("/dict")
def add_dict(p: DictPayload, _: None = Depends(require_auth)):
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
def update_dict(entry_id: int, p: DictPayload, _: None = Depends(require_auth)):
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
def delete_dict(entry_id: int, _: None = Depends(require_auth)):
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM dict_entry WHERE id=?", (entry_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "条目不存在")
    return {"ok": True}


# ---------- 统计 ----------

@router.get("/stats")
def stats(_: None = Depends(require_auth)):
    with get_conn() as conn:
        def group(sql: str):
            return {r[0] or "未填": r[1] for r in conn.execute(sql).fetchall()}
        return {
            "ok": True,
            "total": conn.execute("SELECT COUNT(*) FROM item").fetchone()[0],
            "confirmed": conn.execute(
                "SELECT COUNT(*) FROM item WHERE status!='draft'").fetchone()[0],
            "draft": conn.execute(
                "SELECT COUNT(*) FROM item WHERE status='draft'").fetchone()[0],
            "photos": conn.execute("SELECT COUNT(*) FROM photo").fetchone()[0],
            "est_total": conn.execute(
                "SELECT SUM(est_value) FROM item").fetchone()[0] or 0,
            "by_category": group("SELECT category, COUNT(*) FROM item GROUP BY category"),
            "by_status": group("SELECT status, COUNT(*) FROM item GROUP BY status"),
            "by_series": group("SELECT series, COUNT(*) FROM item GROUP BY series"),
            "by_grade_co": group(
                "SELECT grade_co, COUNT(*) FROM item GROUP BY grade_co "
                "ORDER BY COUNT(*) DESC LIMIT 10"),
        }


# ---------- Excel 导出 ----------

@router.get("/export/xlsx")
def export_xlsx(_: None = Depends(require_auth)):
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
