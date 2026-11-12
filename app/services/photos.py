"""照片入库：规范化保存（压到长边 2000px）、EXIF 时间、aHash 指纹查重。"""
import io
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from PIL import Image

from ..config import PHOTO_DIR

MAX_DIM = 2000
# 白名单：既防手滑，也防 shot_type 拼进文件名造成路径穿越
SHOT_TYPES = ("front", "back", "edge", "detail", "label", "holder", "reference")


def ahash(img: Image.Image) -> Optional[str]:
    """8x8 均值哈希，同图重复入库检测用（容忍轻微压缩差异）。"""
    try:
        small = img.convert("L").resize((8, 8), Image.LANCZOS)
        px = list(small.getdata())
        avg = sum(px) / 64
        bits = "".join("1" if v > avg else "0" for v in px)
        return f"{int(bits, 2):016x}"
    except Exception:
        return None


def _exif_time(img: Image.Image) -> Optional[str]:
    try:
        exif = img.getexif()
        for tag in (36867, 306):  # DateTimeOriginal, DateTime
            v = exif.get(tag)
            if v:
                return str(v)
    except Exception:
        pass
    return None


def save_image(data: bytes, item_id: int, shot_type: str,
               origin: str = "archive") -> Optional[dict]:
    """保存一张照片，返回 photo 表字段 dict；图片无效返回 None。"""
    if shot_type not in SHOT_TYPES:   # 不在白名单一律按"局部"处理，杜绝路径穿越
        shot_type = "detail"
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:
        return None
    exif_time = _exif_time(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    if max(img.size) > MAX_DIM:
        img.thumbnail((MAX_DIM, MAX_DIM), Image.LANCZOS)

    d = PHOTO_DIR / str(item_id)
    d.mkdir(parents=True, exist_ok=True)
    fname = f"{shot_type}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]}.jpg"
    path = d / fname
    img.save(path, "JPEG", quality=85)
    return {
        "shot_type": shot_type,
        "origin": origin,
        # URL 用正斜杠（Windows 上 relative_to 会给出反斜杠）
        "file_path": str(path.relative_to(PHOTO_DIR)).replace("\\", "/"),
        "exif_time": exif_time,
        "phash": ahash(img),
    }


def ensure_thumb(rel_path: str) -> Optional[str]:
    """为已存照片生成/复用 480px 缩略图（同目录 thumbs/ 下），返回相对路径。

    列表卡片流用缩略图，点开详情才加载原图——3000 件的列表秒开。
    """
    src = PHOTO_DIR / rel_path   # Path 兼容正斜杠（含 Windows）
    if not src.exists():
        return None
    dst = src.parent / "thumbs" / (src.stem + ".jpg")
    rel = str(dst.relative_to(PHOTO_DIR)).replace("\\", "/")
    if dst.exists():
        return rel
    try:
        img = Image.open(src)
        img.load()
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.thumbnail((480, 480), Image.LANCZOS)
        dst.parent.mkdir(parents=True, exist_ok=True)
        img.save(dst, "JPEG", quality=82)
        return rel
    except Exception:
        return None


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def find_similar(phash: Optional[str], exclude_item_id: Optional[int] = None,
                 max_dist: int = 4, limit: int = 5,
                 conn: Optional[sqlite3.Connection] = None) -> List[dict]:
    """在已入库照片里找指纹相近的，提示疑似重复。"""
    if not phash or not conn:
        return []
    rows = conn.execute(
        """SELECT p.item_id, p.file_path, p.phash, i.item_code, i.name, i.cert_no
           FROM photo p JOIN item i ON i.id = p.item_id
           WHERE p.phash IS NOT NULL AND p.item_id != ?""",
        (exclude_item_id or 0,),
    ).fetchall()
    hits = []
    for r in rows:
        dist = hamming(phash, r["phash"])
        if dist <= max_dist:
            hits.append({"item_id": r["item_id"], "item_code": r["item_code"],
                         "name": r["name"], "cert_no": r["cert_no"], "distance": dist})
    hits.sort(key=lambda h: h["distance"])
    return hits[:limit]
