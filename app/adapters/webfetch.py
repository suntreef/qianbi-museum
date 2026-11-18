"""限频抓取证书页：SSRF 防护（含重定向逐跳校验 + DNS 解析校验）、文本/图片提取。

安全设计：
- 只允许公网 http(s) 地址，拒绝内网/环回/链路本地字面量
- 域名解析出的每个 IP 都必须是公网（防 DNS 重绑定指向内网）
- 重定向不自动跟随：每一跳重新过公网校验，最多 3 跳
- 响应体限量读取；图片过滤明显的站点装饰资源
"""
import ipaddress
import re
import socket
import time
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx

from ..config import FETCH_INTERVAL_SECONDS

_UA = {"User-Agent": "Mozilla/5.0 (compatible; QianbiMuseum/0.1; private museum archive)"}
_TIMEOUT = 15.0
_MAX_HTML_BYTES = 3_000_000
_MAX_IMG_BYTES = 8_000_000
_MAX_REDIRECTS = 3

_last_fetch = 0.0
_img_re = re.compile(
    r"""<img[^>]+src\s*=\s*["']([^"']+)["']""", re.I)
_junk_img = re.compile(
    r"(head|logo|icon|banner|btn|/bg|_bg|qrcode?|no-senditem|pjtz|wenhao|link-info|star-line)", re.I)
_spa_tpl_re = re.compile(r"\{\{[^{}]{1,80}\}\}")


def _ip_is_public(ip: ipaddress.IPv4Address) -> bool:
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast)


def _host_ok(host: str) -> bool:
    if host == "localhost" or host.endswith(".local") or host.endswith(".internal"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return _ip_is_public(ip)
    except ValueError:
        # 域名：解析出的每个 IP 都必须是公网（防 DNS 重绑定）
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            return False
        if not infos:
            return False
        return all(_ip_is_public(ipaddress.ip_address(info[4][0])) for info in infos)


def is_public_url(url: str) -> bool:
    """只允许公网 http(s)：防二维码把内网地址喂给服务器（SSRF）。"""
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    return _host_ok(u.hostname.lower())


def _throttle() -> None:
    global _last_fetch
    wait = FETCH_INTERVAL_SECONDS - (time.time() - _last_fetch)
    if wait > 0:
        time.sleep(wait)
    _last_fetch = time.time()


def fetch_page(url: str, depth: int = 0) -> Optional[dict]:
    """限频抓取页面，返回 {url_final, html}；失败/非公网/重定向过深返回 None。"""
    if depth > _MAX_REDIRECTS or not is_public_url(url):
        return None
    _throttle()
    try:
        with httpx.Client(headers=_UA, timeout=_TIMEOUT,
                          follow_redirects=False) as c:
            resp = c.get(url)
            if resp.is_redirect:
                nxt = urljoin(url, resp.headers.get("location", ""))
                resp.close()
                return fetch_page(nxt, depth + 1)   # 每一跳重新过公网校验
            if resp.status_code != 200:
                return None
            html = resp.text
            if len(html) > _MAX_HTML_BYTES:
                html = html[:_MAX_HTML_BYTES]
        return {"url_final": str(resp.url), "html": html}
    except Exception:
        return None


def extract_text(html: str) -> str:
    """粗提取可见文本：去掉 script/style/标签，折叠空白。"""
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", text).strip()


def is_spa_shell(text: str) -> bool:
    """识别纯前端动态渲染空壳页（如华夏 querySendItem.html 返回 200 但全是 {{QueryResult.xxx}}）。
    命中空壳时不应把模板文字和占位图当真证书页，应退回 URL 提取 + 标签 OCR。"""
    if not text or len(text.strip()) < 6:
        return True
    return len(_spa_tpl_re.findall(text)) >= 2


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


def fetch_image(url: str, depth: int = 0) -> Optional[bytes]:
    if depth > _MAX_REDIRECTS or not is_public_url(url):
        return None
    _throttle()
    try:
        with httpx.Client(headers=_UA, timeout=_TIMEOUT,
                          follow_redirects=False) as c:
            resp = c.get(url)
            if resp.is_redirect:
                nxt = urljoin(url, resp.headers.get("location", ""))
                resp.close()
                return fetch_image(nxt, depth + 1)
            if resp.status_code != 200 or len(resp.content) > _MAX_IMG_BYTES:
                return None
            return resp.content
    except Exception:
        return None
