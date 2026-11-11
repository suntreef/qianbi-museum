"""限频抓取证书页：SSRF 防护、文本提取、图片 URL 提取、图片下载。"""
import re
import time
from typing import Optional
from urllib.parse import urljoin, urlparse
import ipaddress

import httpx

from ..config import FETCH_INTERVAL_SECONDS

_UA = {"User-Agent": "Mozilla/5.0 (compatible; QianbiMuseum/0.1; private museum archive)"}
_TIMEOUT = 15.0
_MAX_HTML_BYTES = 3_000_000
_MAX_IMG_BYTES = 8_000_000

_last_fetch = 0.0
_img_re = re.compile(
    r"""<img[^>]+src\s*=\s*["']([^"']+)["']""", re.I)
_junk_img = re.compile(r"(head|logo|icon|banner|btn|/bg|_bg|qrcode?)", re.I)


def is_public_url(url: str) -> bool:
    """只允许公网 http(s)：防二维码把内网地址喂给服务器（SSRF）。"""
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    host = u.hostname.lower()
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast)
    except ValueError:
        return True  # 普通域名


def _throttle() -> None:
    global _last_fetch
    wait = FETCH_INTERVAL_SECONDS - (time.time() - _last_fetch)
    if wait > 0:
        time.sleep(wait)
    _last_fetch = time.time()


def fetch_page(url: str) -> Optional[dict]:
    """限频抓取页面，返回 {url_final, html}；失败/非公网返回 None。"""
    if not is_public_url(url):
        return None
    _throttle()
    try:
        with httpx.Client(headers=_UA, timeout=_TIMEOUT, follow_redirects=True) as c:
            with c.stream("GET", url) as resp:
                if resp.status_code != 200:
                    return None
                final_url = str(resp.url)
                chunks, total = [], 0
                for chunk in resp.iter_bytes():
                    total += len(chunk)
                    chunks.append(chunk)
                    if total >= _MAX_HTML_BYTES:
                        break
        return {"url_final": final_url, "html": b"".join(chunks).decode("utf-8", "replace")}
    except Exception:
        return None


def extract_text(html: str) -> str:
    """粗提取可见文本：去掉 script/style/标签，折叠空白。"""
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def extract_image_urls(html: str, base_url: str, limit: int = 6) -> list:
    urls = []
    for src in _img_re.findall(html):
        if src.startswith("data:") or _junk_img.search(src):
            continue
        full = urljoin(base_url, src)
        if is_public_url(full) and full not in urls:
            urls.append(full)
        if len(urls) >= limit:
            break
    return urls


def fetch_image(url: str) -> Optional[bytes]:
    if not is_public_url(url):
        return None
    _throttle()
    try:
        with httpx.Client(headers=_UA, timeout=_TIMEOUT, follow_redirects=True) as c:
            resp = c.get(url)
            if resp.status_code != 200 or len(resp.content) > _MAX_IMG_BYTES:
                return None
            return resp.content
    except Exception:
        return None
