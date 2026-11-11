"""二维码解码：从拍的照片里读出证书页 URL（本地 zxing-cpp，免费）。

解码策略：原图 → 灰度 → 灰度放大2x → 顶部裁切放大3x（评级标签条通常在照片上方），
逐个尝试，兼容拍糊、距离远、标签小的照片。
"""
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
            img.load()
            gray = img.convert("L")
            variants = [img, gray]
            big = gray.resize((gray.width * 2, gray.height * 2), Image.LANCZOS)
            variants.append(big)
            top = gray.crop((0, 0, gray.width, max(1, gray.height // 3)))
            variants.append(top.resize((top.width * 3, top.height * 3), Image.LANCZOS))
            for im in variants:
                try:
                    results = zxingcpp.read_barcodes(im)
                except Exception:
                    continue
                for r in results:
                    text = (getattr(r, "text", "") or "").strip()
                    if text:
                        return text
    except Exception:
        return None
    return None
