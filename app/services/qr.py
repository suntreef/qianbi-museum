"""二维码解码：从拍的照片里读出证书页 URL（本地 zxing-cpp，免费）。

解码策略（逐级增强，实测自 PMG/国标真实标签照片）：
原图 → 灰度 → 放大2x → 顶部裁切3x → 右上角裁切4~6x（PMG 等评级标签的小
二维码在右上角，直接拍常解不出，裁切放大后可解）。
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
            w, h = gray.size
            variants = [img, gray]
            big = gray.resize((w * 2, h * 2), Image.LANCZOS)
            variants.append(big)
            top = gray.crop((0, 0, w, max(1, h // 3)))
            variants.append(top.resize((top.width * 3, top.height * 3), Image.LANCZOS))
            # 右上角裁切多倍放大（评级标签小码）
            for frac, scale in ((0.55, 6), (0.45, 8), (0.30, 10)):
                c = gray.crop((int(w * (1 - frac)), 0, w, max(1, int(h * 0.4))))
                variants.append(c.resize((c.width * scale, c.height * scale),
                                         Image.LANCZOS))
            # 竖拍横盒兜底（90°侧拍时小码落在右下角/左上角）
            if h > w * 1.15:
                rot = gray.transpose(Image.Transpose.ROTATE_90)
                rw, rh = rot.size
                rc = rot.crop((int(rw * 0.55), 0, rw, max(1, int(rh * 0.4))))
                variants.append(rc.resize((rc.width * 6, rc.height * 6), Image.LANCZOS))
            fallback = None
            for im in variants:
                try:
                    results = zxingcpp.read_barcodes(im)
                except Exception:
                    continue
                for r in results:
                    text = (getattr(r, "text", "") or "").strip()
                    if text.startswith(("http://", "https://")):
                        return text
                    if text and fallback is None:
                        fallback = text
            return fallback
    except Exception:
        return None
    return None
