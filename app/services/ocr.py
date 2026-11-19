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
                # 竖拍横盒（如闻德 768x1024 顺时针90°侧拍）：逆时针转正后再裁顶部标签区
                if h > w * 1.15 and w >= 300:
                    rot = im.transpose(Image.Transpose.ROTATE_90)
                    rw, rh = rot.size
                    rot_top = rot.crop((0, 0, rw, max(1, int(rh * 0.42))))
                    r_res, _ = eng(rot_top)
                    for t in _extract_lines(r_res):
                        if t not in seen:
                            seen.add(t)
                            parts.append(t)
                elif h > w * 0.5 and h >= 300:
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


# ---------- 第二引擎：Indra OCR（可选，速度 15~38 倍，用于批量字段互补） ----------
# 实测（2026-11 真实标签）：速度 0.2~0.5s/张（RapidOCR 7~9s）；
# 英文拼写质量更高（Tsingtao 正确），但中文密集小字漏检多——
# 不替换主引擎，仅在批量队列中与 RapidOCR 双跑做字段互补。
_indra_conv = None
_indra_tried = False


def indra_available() -> bool:
    """Indra 可用性：库已安装且模型加载成功（首次会从 HuggingFace 下载，
    国内环境可设环境变量 HF_ENDPOINT=https://hf-mirror.com 加速）。"""
    global _indra_conv, _indra_tried
    if _indra_tried:
        return _indra_conv is not None
    _indra_tried = True
    try:
        import indra_ocr
        _indra_conv = indra_ocr.load(threads=4)
    except Exception:
        _indra_conv = None
    return _indra_conv is not None


def read_text_indra(image_path):
    """Indra 引擎：返回 (text, avg_conf)；不可用/失败返回 (None, 0)。"""
    if not indra_available():
        return None, 0
    try:
        import cv2
        gray = cv2.cvtColor(cv2.imread(str(image_path)), cv2.COLOR_BGR2GRAY)
        page = _indra_conv.ocr(gray)
        text = (page.text or "").strip()
        lines = page.lines or []
        conf = round(sum(l.conf for l in lines) / len(lines), 2) if lines else 0
        return (text or None), conf
    except Exception:
        return None, 0
