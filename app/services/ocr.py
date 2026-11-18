"""本地免费 OCR（RapidOCR，ONNX/CPU）。缺依赖时自动降级返回 None，不影响主流程。"""
from typing import Optional

_engine = None
_checked = False


def available() -> bool:
    global _checked
    if not _checked:
        try:
            import rapidocr_onnxruntime  # noqa: F401
            _checked = True
        except ImportError:
            _checked = False
    return _checked


def _get():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def _extract_lines(raw) -> list:
    if not raw:
        return []
    return [str(item[1]).strip() for item in raw
            if item and len(item) > 1 and str(item[1]).strip()]


def read_text(image_path) -> Optional[str]:
    """识别图片中的文字，返回空格拼接的文本；不可用/失败返回 None。

    增强策略（实测自华夏整盒照片）：整盒照直接跑 OCR 时，标签右侧竖排小字 EPQ 易与
    大号数字合并误识（53+E→535）；先裁顶部 42% 标签区识别并置顶，再拼全图补充行。
    """
    if not available():
        return None
    try:
        eng = _get()
        parts: list = []
        seen: set = set()
        try:
            from PIL import Image
            with Image.open(image_path) as im:
                w, h = im.size
                if h > w * 0.5 and h >= 300:
                    top = im.crop((0, 0, w, max(1, int(h * 0.42))))
                    top_res, _ = eng(top)
                    for t in _extract_lines(top_res):
                        if t not in seen:
                            seen.add(t)
                            parts.append(t)
        except Exception:
            pass
        full_res, _ = eng(str(image_path))
        for t in _extract_lines(full_res):
            if t not in seen:
                seen.add(t)
                parts.append(t)
        return " ".join(parts) if parts else None
    except Exception:
        return None
