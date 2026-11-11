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


def read_text(image_path) -> Optional[str]:
    """识别图片中的文字，返回空格拼接的文本；不可用/失败返回 None。"""
    if not available():
        return None
    try:
        result, _ = _get()(str(image_path))
        if not result:
            return None
        return " ".join(str(item[1]) for item in result
                        if item and len(item) > 1 and item[1])
    except Exception:
        return None
