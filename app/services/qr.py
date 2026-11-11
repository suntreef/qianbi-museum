"""二维码解码：从拍的照片里读出证书页 URL（本地 zxing-cpp，免费）。"""
from typing import Optional

try:
    import zxingcpp
    from PIL import Image
    _OK = True
except ImportError:  # 缺依赖时降级，不影响其他功能
    _OK = False


def decode_qr(image_path) -> Optional[str]:
    """返回二维码内容字符串；没有码或读不出返回 None。"""
    if not _OK:
        return None
    try:
        with Image.open(image_path) as img:
            results = zxingcpp.read_barcodes(img)
        for r in results:
            text = (getattr(r, "text", "") or "").strip()
            if text:
                return text
    except Exception:
        return None
    return None
