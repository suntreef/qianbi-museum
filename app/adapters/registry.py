"""域名 → 评级公司台账：从二维码 URL 识别评级公司（用于自动填 grade_co）。

内置常用的几家；更多公司后台在字典里登记（type='cert_domain'，name=域名，
aliases=公司名）即可生效，不用改代码。
"""
from typing import Optional
from urllib.parse import urlparse

from ..models import get_conn

BUILTIN_DOMAINS = {
    "cfcr.org.cn": "国标",          # NSCA 国标文物评级（实测 DZB 编号体系）
    "pmgnotes.com": "PMG",
    "pmgcert.com": "PMG",
    "notes.pmgcert.com": "PMG",
    "ngccoin.com": "NGC",
    "pcgs.com": "PCGS",
    "gbca.cn": "公博",
}


def _host(url: str) -> Optional[str]:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return None
    return host[4:] if host.startswith("www.") else host


def company_for_url(url: str) -> Optional[str]:
    if not url:
        return None
    host = _host(url)
    if not host:
        return None
    for dom, co in BUILTIN_DOMAINS.items():
        if host == dom or host.endswith("." + dom):
            return co
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT name, aliases FROM dict_entry WHERE type='cert_domain'").fetchall()
    for r in rows:
        dom = (r["name"] or "").lower()
        if dom and (host == dom or host.endswith("." + dom)):
            return r["aliases"] or r["name"]
    return None
