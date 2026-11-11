"""规则解析（免费主干）：从证书页文本 / URL / 标签 OCR 文本提取字段。

三路解析：
1. 字段标签解析（藏品编号/名称/年代/面值/冠号/分数）——国内多数评级公司证书页是这种格式
2. 字典自动匹配——机构、券种、印刷厂的中英文名（aliases）做"最长命中"匹配
3. 通用正则——分数（MS 63 / 60EPQ / 美品78）、编号（纯数字 / 字母+数字）、纪年换算

解析不出的字段留空，交给确认卡人工补齐（宁空勿错）。
"""
import re
from typing import Optional

from ..models import get_conn
from ..services import years

# ---------- 分数 ----------
_GRADE_70 = re.compile(
    r"\b(GEM ?BU|MS|UNC|AU|XF|EF|VF|F|VG|G|AG|PR|PF|SP)\s*-?\s*(\d{1,2})\b", re.I)
_GRADE_BARE_EPQ = re.compile(r"\b(\d{1,2})\s*(EPQ|PPQ)\b", re.I)  # 国标风格：60EPQ
_GRADE_CN_NUM = re.compile(r"(\d{2})\s*分")
_GRADE_CN_DESC = re.compile(r"(未使用|近未使用|极美品?|美品|上品|普品|全品)")
_EPQ = re.compile(r"\b(EPQ|PPQ)\b")
_DENOM = re.compile(r"[壹贰叁肆伍陆柒捌玖拾佰仟万亿0-9]{1,8}\s*[圆元]")
_YEAR = re.compile(
    r"(民国|洪武|永乐|嘉靖|万历|顺治|康熙|雍正|乾隆|嘉庆|道光|咸丰|同治|光绪|宣统)"
    r"\s*(元|\d{1,3}|[一二三四五六七八九十廿卅]+)\s*年")

# ---------- 编号 ----------
_CERT_DIGITS_TEXT = re.compile(r"(?<![A-Za-z0-9])(\d{7,12})(?![0-9])")
_CERT_ALNUM_TEXT = re.compile(r"\b([A-Z]{1,5}\d{7,16})\b")  # 如 DZB2607050045

# ---------- 字段标签（国内评级公司证书页通用格式） ----------
_LABELED = [
    ("cert_no", r"藏品编号\s*[:：]?\s*([A-Za-z0-9\-]{4,24})"),
    ("name", r"藏品名称\s*[:：]?\s*(.+?)(?=\s*藏品|\s*数据|$)"),
    ("year_text", r"藏品年代\s*[:：]?\s*([^\s]{2,16})"),
    ("denomination", r"藏品面值\s*[:：]?\s*([^\s]{1,12})"),
    ("serial_no", r"藏品冠号\s*[:：]?\s*([A-Za-z0-9\-]{1,24})"),
    ("grade_raw", r"藏品分数\s*[:：]?\s*([A-Za-z0-9+\-]{1,10})"),
]

# 券种 → 时代系列 推断
_NT_SERIES = {
    "宝钞": "明清票券", "官票": "明清票券", "钱票": "明清票券",
    "法币": "民国纸币", "金圆券": "民国纸币", "银元券": "民国纸币",
    "关金券": "民国纸币", "国币券": "民国纸币", "兑换券": "民国纸币",
    "辅币券": "民国纸币", "军用票": "民国纸币",
    "边币": "边区·解放区",
}

_CN_NUM = re.compile(r"^[0-9]{1,2}$")


def _guess_scale(raw: str) -> Optional[str]:
    """从分数原文猜分数制。只做保守判断，猜不出留空。"""
    if not raw:
        return None
    if re.search(r"EPQ|PPQ", raw, re.I):
        return "70制"
    if re.match(r"^(GEM|MS|UNC|AU|XF|EF|VF|PR|PF|SP)", raw, re.I):
        return "70制"
    if _CN_NUM.match(raw) and int(raw) <= 70:
        return "70制"
    if "分" in raw:
        return "85制"
    if raw in ("未使用", "近未使用", "极美", "极美品", "美品", "上品", "普品", "全品"):
        return "描述"
    return None


def _cands(name: str, aliases: Optional[str]) -> list:
    out = [name]
    if aliases:
        out += [a.strip() for a in aliases.split(",") if a.strip()]
    return out


def _best_match(rows, text: str):
    """挑"最长命中"的条目：避免 'Bank of China'（中国银行）抢走
    'Central Bank of China'（中央银行）这种子串误匹配。等长时 sort 小者优先。"""
    best, best_key = None, None
    for r in rows:
        for c in _cands(r["name"], r["aliases"]):
            if c and c in text:
                key = (len(c), -(r["sort"] or 0))
                if best_key is None or key > best_key:
                    best, best_key = r, key
    return best


def parse_labeled_fields(text: str) -> dict:
    """「藏品XX：值」字段标签解析（国标等国内公司证书页格式）。"""
    out = {}
    for key, pat in _LABELED:
        m = re.search(pat, text)
        if m:
            v = m.group(1).strip()
            if v:
                out[key] = v
    if out.get("year_text"):
        g = years.to_gregorian(out["year_text"])
        if g:
            out["year_sort"] = g
    if out.get("grade_raw"):
        scale = _guess_scale(out["grade_raw"])
        if scale:
            out["grade_scale"] = scale
    return out


def match_dicts(text: str) -> dict:
    """把文本与字典做匹配，返回 {bank, note_type, printer, doc_type, series?}。"""
    out: dict = {}
    if not text:
        return out
    with get_conn() as conn:
        bank_sort = None
        for ftype, key in (("bank", "bank"), ("note_type", "note_type"),
                           ("printer", "printer"), ("doc_type", "doc_type")):
            rows = conn.execute(
                "SELECT name, aliases, sort FROM dict_entry WHERE type=? ORDER BY sort, id",
                (ftype,)).fetchall()
            hit = _best_match(rows, text)
            if hit:
                out[key] = hit["name"]
                if ftype == "bank":
                    bank_sort = hit["sort"]
        # 时代系列推断（机构分组 sort：1-53 民国，60-79 边区，100-102 明清）
        series = None
        if out.get("bank") == "中国人民银行":
            series = None  # 第几套人民币需人工选
        elif bank_sort is not None:
            if 100 <= bank_sort <= 102:
                series = "明清票券"
            elif 60 <= bank_sort <= 79:
                series = "边区·解放区"
            elif 1 <= bank_sort <= 53:
                series = "民国纸币"
        if not series and out.get("note_type"):
            series = _NT_SERIES.get(out["note_type"])
        if series:
            out["series"] = series
    return out


def parse_grade(text: str) -> dict:
    out = {}
    m = _GRADE_70.search(text)
    if m:
        raw = f"{m.group(1).upper()} {m.group(2)}"
        if _EPQ.search(text):
            raw += " EPQ"
        out.update({"grade_raw": raw, "grade_scale": "70制"})
        return out
    m = _GRADE_BARE_EPQ.search(text)
    if m:
        out.update({"grade_raw": f"{m.group(1)} {m.group(2).upper()}", "grade_scale": "70制"})
        return out
    m = _GRADE_CN_NUM.search(text)
    if m:
        out.update({"grade_raw": f"{m.group(1)}分", "grade_scale": "85制"})
        return out
    m = _GRADE_CN_DESC.search(text)
    if m:
        out.update({"grade_raw": m.group(1), "grade_scale": "描述"})
    return out


def _cert_no_from_url(url: str) -> Optional[str]:
    m = (re.search(r"(?:cert(?:ificate)?|sn|serial|number|no\.?|id)[=/#]([0-9]{4,15})",
                   url, re.I)
         or re.search(r"[/?#]([A-Z]{1,5}\d{7,16})(?:[/?#]|$)", url)
         or re.search(r"[/?#](\d{6,15})(?:[/?#]|$)", url))
    return m.group(1) if m else None


def _finish(fields: dict) -> dict:
    text = fields.get("_text", "")
    if not fields.get("denomination"):
        m = _DENOM.search(text)
        if m:
            fields["denomination"] = m.group(0).replace(" ", "")
    if not fields.get("year_text"):
        m = _YEAR.search(text)
        if m:
            fields["year_text"] = m.group(0)
            g = years.to_gregorian(m.group(0))
            if g:
                fields["year_sort"] = g
    fields.pop("_text", None)
    if not fields.get("grade_raw"):
        fields.pop("grade_scale", None)
    if not fields.get("name"):
        parts = [fields.get("bank"), fields.get("note_type"),
                 fields.get("denomination"), fields.get("era")]
        name = " ".join(str(p) for p in parts if p)
        if name:
            fields["name"] = name
    return fields


def parse_cert_page(text: str, url: str) -> dict:
    """证书页文本 + 最终 URL → 字段草稿。解析不出的留空。"""
    fields = {"_text": text}
    cert_no = _cert_no_from_url(url)
    if cert_no:
        fields["cert_no"] = cert_no
    fields.update(parse_labeled_fields(text))
    fields.update(match_dicts(text))
    if not fields.get("grade_raw"):
        fields.update(parse_grade(text))
    return _finish(fields)


def parse_label_ocr(text: str) -> dict:
    """标签 OCR 文本 → 字段草稿。评级编号从字母+数字或长数字串里猜。"""
    fields = {"_text": text}
    fields.update(parse_labeled_fields(text))
    fields.update(match_dicts(text))
    if not fields.get("grade_raw"):
        fields.update(parse_grade(text))
    if not fields.get("cert_no"):
        m = _CERT_ALNUM_TEXT.search(text) or _CERT_DIGITS_TEXT.search(text)
        if m:
            fields["cert_no"] = m.group(1)
    return _finish(fields)
