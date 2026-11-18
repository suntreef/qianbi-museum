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
# 60EPQ / 62 EPO / 53 EPC（EPO/EPC 为 RapidOCR 对横排/竖排 EPQ 的常见误识）
_GRADE_BARE_EPQ = re.compile(r"\b(\d{1,2})\s*(EPQ|EPO|EPC|PPQ)\b", re.I)
# 华夏等标签 EPQ 竖排在大号分数右侧且上沿略高，OCR 排序后呈 "EPO 53" / "EPC 53"
_GRADE_EPQ_FIRST = re.compile(r"\b(EPQ|EPO|EPC|PPQ)\s*(\d{1,2})\b", re.I)
_GRADE_EN_DESC = re.compile(
    r"\b(\d{2})\s*(About\s*Un\w+|Uncirc\w*|Unc\w* circ\w*)", re.I)  # PMG 风格：53 About Uncirculated
_GRADE_CN_NUM = re.compile(r"(\d{2})\s*分")
_GRADE_CN_DESC = re.compile(r"(未使用|近未使用|极美品?|美品|上品|普品|全品)")
_EPQ = re.compile(r"\b(EPQ|PPQ)\b")
# 中文面值：圆/元/角允许汉字或阿拉伯数字；「分」仅限汉字数字或 1/2/5 分，严防误吞古钱「82分」等级！
_DENOM = re.compile(
    r"[壹贰叁肆伍陆柒捌玖拾佰仟万亿一二三四五六七八九十0-9]{1,8}\s*[圆元角]"
    r"|(?:[壹贰叁肆伍陆柒捌玖拾佰仟一二三四五六七八九十]{1,6}|(?<!\d)[125])\s*分(?![0-9])"
    r"|[壹贰叁肆伍陆柒捌玖拾佰仟一二三四五六七八九十]{1,6}\s*[厘文贯两]"
)
_YEAR = re.compile(
    r"((?:中华)?民国|洪武|永乐|嘉靖|万历|顺治|康熙|雍正|乾隆|嘉庆|道光|咸丰|同治|光绪|宣统)"
    r"\s*(元|\d{1,3}|[一二三四五六七八九十廿卅]+)\s*年|((?<!\d)(1[89]\d{2}|20[0-4]\d))\s*年")
# 英文标签常不写「年」字（如 PCGS "1934 10Cents"、PMG "1949 5 Cents"）
_EN_YEAR_BEFORE_DENOM = re.compile(
    r"(?<![0-9A-Za-z/-])(18[5-9]\d|19\d{2}|20[0-3]\d)(?=\s*\b\d+\s*(?:Cents?|Yuan|Dollars?)\b)",
    re.I)

# ---------- 编号 ----------
_CERT_DIGITS_TEXT = re.compile(r"(?<![A-Za-z0-9./])(\d{7,12})(?![0-9./])")
# 混合字母+数字评级编号：国标 DZB2607050045、保粹 B3735A3583、7788 70002207725F（8~18位且至少7位数字）
_CERT_ALNUM_TEXT = re.compile(
    r"\b((?=[A-Z0-9]{8,18}\b)(?=[A-Z0-9]*[A-Z])(?=(?:[A-Z]*\d){7})[A-Z0-9]{8,18})\b")
# PCGS 专属条码行：{品种号}.{分数}/{证书号}，如 1147710.45/88052280
_PCGS_BARCODE = re.compile(r"\b(\d{5,8})\.(\d{2})/(\d{7,10})\b")
_PCGS_GRADE_PFX = re.compile(r"\b(CHOICE|GEM)\s*(UNC|BU|AU|XF|EF|VF|F|VG)\b", re.I)
# 评级标签裸分数（如保粹 65、7788 60，不带 EPQ/UNC 后缀）：仅在确认有评级公司标识时启用
_CO_HINT = re.compile(r"(?:评级|GUARANTY|guarantee|BANKNOTE|PCGS|PMG|NGC|CCGA|ACG|7788)", re.I)
_SHELDON_BARE = re.compile(r"(?:^|\s)(12|15|20|25|30|35|40|45|50|53|55|58|6[0-9]|70)(?=\s|$)")

# ---------- 字段标签（国内评级公司证书页通用格式） ----------
_LABELED = [
    ("cert_no", r"藏品编号\s*[:：]?\s*([A-Za-z0-9\-]{4,24})"),
    ("name", r"藏品名称\s*[:：]?\s*(.+?)(?=\s*藏品|\s*数据|$)"),
    ("year_text", r"藏品年代\s*[:：]?\s*([^\s]{2,16})"),
    ("denomination", r"藏品面值\s*[:：]?\s*([^\s]{1,12})"),
    ("serial_no", r"藏品冠号\s*[:：]?\s*([A-Za-z0-9\-]{1,24})"),
    ("grade_raw", r"藏品分数\s*[:：]?\s*([A-Za-z0-9+\-]{1,10})"),
]

# 目录号（Pick#/S/M#，PMG/PCGS 标签标准）；支持省钞 S 前缀与小写版别后缀（Pick#S1139a）及粘连容错
_CATALOG = re.compile(r"(?i:Pick)\s*#?\s*([A-Z]?[0-9]{1,5}[a-z]?)(?![0-9])")
_CATALOG_SM = re.compile(
    r"(?i:S/M)\s*#?\s*([A-Z]{1,2}\s?[0-9]{1,4}(?:-(?:[0-9]{1,3}[a-z]|[0-9]{1,2}(?!\d)))?)")
# 冠号：PMG S/N C072709；PCGS Serial#L0325587Printer:TTBC；国内 冠号：1-B755211 / 冠号S2761545
_SN_PMG = re.compile(r"(?:S/N|Serial\s*#?)\s*([A-Z]{0,3}\s?[0-9]{4,10})", re.I)
_SN_ACG = re.compile(r"冠号\s*[：:]?\s*([A-Z]{0,3}[-—]?\s?[0-9A-Z-]{4,14})", re.I)
# 英文面值 → 中文（PMG/PCGS 标签；中国纸币 10/20/50 Cents 对应 壹角/贰角/伍角）
_EN_DENOM = [
    (re.compile(r"(\d+)\s*Cents?", re.I), "分"),
    (re.compile(r"(\d+)\s*Yuan", re.I), "圆"),
    (re.compile(r"(\d+)\s*Dollars?", re.I), "圆"),
]
_CENTS_JIAO = {"10": "壹角", "20": "贰角", "50": "伍角"}
_CN_NUM_MAP = {"1": "壹", "2": "贰", "3": "叁", "5": "伍", "10": "拾",
               "20": "贰拾", "50": "伍拾", "100": "壹佰",
               "500": "伍佰", "1000": "壹仟", "2000": "贰仟", "10000": "壹万"}

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
    if re.match(r"^(GEM|CHOICE|MS|UNC|AU|XF|EF|VF|PR|PF|SP)", raw, re.I):
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


def parse_label_extras(text: str) -> dict:
    """标签/证书文本里的目录号、冠号、版别特征、英文面值提取（真实样本实测补充）。"""
    out = {}
    m = _CATALOG.search(text)
    if m:
        ref = f"Pick# {m.group(1)}"
        m2 = _CATALOG_SM.search(text)
        if m2:
            ref += f"  S/M#{m2.group(1).replace(' ', '')}"
        out["catalog_ref"] = ref
    m = _SN_PMG.search(text)
    if m:
        out["serial_no"] = m.group(1).replace(" ", "")
    else:
        m = _SN_ACG.search(text)
        if m:
            out["serial_no"] = m.group(1).replace(" ", "")
    m_yr = re.search(r"(?<!藏品)年代\s*[:：]\s*([^\s（(]{2,16})", text)
    if m_yr:
        out["year_text"] = m_yr.group(1)
        g = years.to_gregorian(out["year_text"])
        if g:
            out["year_sort"] = g
    m_var = re.search(r"特征\s*[:：]\s*([^\s]{2,24})", text)
    if m_var:
        out["variety"] = m_var.group(1).strip("，,。;；")
    for pat, unit in _EN_DENOM:
        for m in pat.finditer(text):
            n = m.group(1)
            if len(n.lstrip("0")) > 4 or int(n) > 10000:
                continue   # OCR 粘连年份+面值的长串，宁空勿错
            if unit == "分" and n in _CENTS_JIAO:
                out["denomination"] = _CENTS_JIAO[n]
            else:
                cn = _CN_NUM_MAP.get(n, n)
                out["denomination"] = f"{cn}{unit}"
            break
        if out.get("denomination"):
            break
    return out


def match_dicts(text: str) -> dict:
    """把文本与字典做匹配，返回 {bank, note_type, printer, doc_type, grade_co?, series?, category?}。"""
    out: dict = {}
    if not text:
        return out
    with get_conn() as conn:
        bank_sort = None
        for ftype, key in (("bank", "bank"), ("note_type", "note_type"),
                           ("printer", "printer"), ("doc_type", "doc_type"),
                           ("grade_co", "grade_co")):
            rows = conn.execute(
                "SELECT name, aliases, sort FROM dict_entry WHERE type=? ORDER BY sort, id",
                (ftype,)).fetchall()
            if ftype == "grade_co":
                rows = [r for r in rows if r["name"] != "无"]  # 防单字「无」误命中普通文本
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
        if out.get("bank") or out.get("note_type") or out.get("printer"):
            out["category"] = "纸币"
        elif out.get("doc_type"):
            out["category"] = "契约票证"
    return out


def _norm_epq(raw_tag: str) -> str:
    t = raw_tag.upper()
    return "EPQ" if t in ("EPO", "EPC") else t


def parse_grade(text: str, has_company: bool = False) -> dict:
    out = {}
    m = _GRADE_70.search(text)
    if m:
        raw = f"{m.group(1).upper()} {m.group(2)}"
        if _EPQ.search(text):
            raw += " EPQ"
        out.update({"grade_raw": raw, "grade_scale": "70制"})
        return out
    m = _GRADE_BARE_EPQ.search(text)
    if m and 1 <= int(m.group(1)) <= 70:
        out.update({"grade_raw": f"{m.group(1)} {_norm_epq(m.group(2))}",
                    "grade_scale": "70制"})
        return out
    m = _GRADE_EPQ_FIRST.search(text)
    if m and 1 <= int(m.group(2)) <= 70:
        out.update({"grade_raw": f"{m.group(2)} {_norm_epq(m.group(1))}",
                    "grade_scale": "70制"})
        return out
    m = _GRADE_EN_DESC.search(text)
    if m:
        out.update({"grade_raw": m.group(1), "grade_scale": "70制"})
        return out
    m = _GRADE_CN_NUM.search(text)
    if m:
        out.update({"grade_raw": f"{m.group(1)}分", "grade_scale": "85制"})
        return out
    m = _GRADE_CN_DESC.search(text)
    if m:
        out.update({"grade_raw": m.group(1), "grade_scale": "描述"})
        return out
    # 保粹（65）、7788（60）等纸币标签常直接印裸分数（不带 EPQ/UNC）；
    # 仅在确认有评级公司上下文时匹配独立 Sheldon 分数，剔除面值/纪年/目录号防误判
    if has_company or _CO_HINT.search(text):
        cleaned = re.sub(r"(?i:Pick)\s*#?\s*[A-Z0-9a-z-]+", " ", text)
        cleaned = re.sub(r"(?i:S/M)\s*#?\s*[A-Z0-9a-z-]+", " ", cleaned)
        cleaned = re.sub(r"\d+\s*(?:Cents?|Yuan|Dollars?|圆|元|角|分)", " ", cleaned, flags=re.I)
        cleaned = re.sub(r"(?:民国|光绪|宣统)\s*\d+\s*年", " ", cleaned)
        m = _SHELDON_BARE.search(cleaned)
        if m:
            out.update({"grade_raw": m.group(1), "grade_scale": "70制"})
    return out


def _cert_no_from_url(url: str) -> Optional[str]:
    m = (re.search(r"(?:cert(?:ificate)?|sn|serial|number|no\.?|id)[=/#]([0-9]{4,16})",
                   url, re.I)
         or re.search(r"(?:order(?:num|no)?|num|code|bh|q)[=/#]([A-Za-z0-9]{6,20})",
                      url, re.I)
         or re.search(
             r"[/?#]((?=[A-Z0-9]{8,18}(?:[/?#&]|$))(?=[A-Z0-9]*[A-Z])(?=(?:[A-Z]*\d){6})[A-Z0-9]{8,18})(?:[/?#&]|$)",
             url)
         or re.search(r"[/?#](\d{6,16})(?:[/?#&]|$)", url))
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
            fields["year_text"] = (m.group(0) or m.group(3) and m.group(3) + "年")
            g = years.to_gregorian(m.group(0))
            if g:
                fields["year_sort"] = g
        else:
            m_en = _EN_YEAR_BEFORE_DENOM.search(text)
            if m_en:
                fields["year_text"] = f"{m_en.group(1)}年"
                fields["year_sort"] = int(m_en.group(1))
    fields.pop("_text", None)
    if not fields.get("grade_raw"):
        fields.pop("grade_scale", None)
    if not fields.get("category"):
        if fields.get("catalog_ref") or re.search(r"\b(EPQ|PPQ)\b", fields.get("grade_raw") or ""):
            fields["category"] = "纸币"
    if not fields.get("name"):
        parts = [fields.get("bank"), fields.get("note_type"),
                 fields.get("denomination"), fields.get("era")]
        name = " ".join(str(p) for p in parts if p)
        if name:
            fields["name"] = name
    return fields


def parse_cert_url(url: str) -> dict:
    """从证书查询 URL 结构直接提取字段（页面被 Cloudflare 拦或为 SPA 空壳时，URL 拦不住）。
    实测：PMG certlookup/{编号}/{分数}；华夏 /c/{编号}；保粹有证链 /43/198/{编号}；7788 /c/{编号}。"""
    out = {}
    if not url:
        return out
    m = re.search(r"certlookup/([0-9]{6,15})(?:/([0-9]{1,2}))?", url, re.I)
    if m:
        out["cert_no"] = m.group(1)
        if m.group(2):
            out["grade_raw"] = m.group(2)
            out["grade_scale"] = "70制"
        return out
    cert_no = _cert_no_from_url(url)
    if cert_no:
        out["cert_no"] = cert_no
    return out


def parse_cert_page(text: str, url: str) -> dict:
    """证书页文本 + 最终 URL → 字段草稿。解析不出的留空。"""
    fields = {"_text": text}
    cert_no = _cert_no_from_url(url)
    if cert_no:
        fields["cert_no"] = cert_no
    fields.update(parse_labeled_fields(text))
    fields.update(parse_label_extras(text))
    fields.update(match_dicts(text))
    if not fields.get("grade_raw"):
        fields.update(parse_grade(text, has_company=bool(fields.get("grade_co"))))
    return _finish(fields)


def parse_label_ocr(text: str) -> dict:
    """标签 OCR 文本 → 字段草稿。评级编号从专属条码、字母+数字或长数字串里猜（排除纸币冠字号）。"""
    fields = {"_text": text}
    fields.update(parse_labeled_fields(text))
    fields.update(parse_label_extras(text))
    fields.update(match_dicts(text))
    # PCGS 专属条码行：{品种号}.{分数}/{证书号}（即使盾牌标 PCGS 被花纹干扰误识为 PGS/RGS 也能精准定位）
    m_pcgs = _PCGS_BARCODE.search(text)
    if m_pcgs and 1 <= int(m_pcgs.group(2)) <= 70:
        fields.setdefault("cert_no", m_pcgs.group(3))
        fields.setdefault("grade_co", "PCGS")
        if not fields.get("grade_raw"):
            m_pfx = _PCGS_GRADE_PFX.search(text)
            raw_g = f"{m_pfx.group(1).upper()} {m_pfx.group(2).upper()} {m_pcgs.group(2)}" if m_pfx else m_pcgs.group(2)
            if _EPQ.search(text):
                raw_g += " PPQ" if "PPQ" in text.upper() else " EPQ"
            fields.update({"grade_raw": raw_g, "grade_scale": "70制"})
    if not fields.get("grade_raw"):
        fields.update(parse_grade(text, has_company=bool(fields.get("grade_co"))))
    if not fields.get("cert_no"):
        cand_text = re.sub(r"(?:冠号\s*[:：]?|S/N|Serial\s*#?)\s*[A-Za-z0-9\-—]+", " ", text, flags=re.I)
        if fields.get("serial_no"):
            cand_text = cand_text.replace(fields["serial_no"], " ")
        m = _CERT_ALNUM_TEXT.search(cand_text) or _CERT_DIGITS_TEXT.search(cand_text)
        if m:
            fields["cert_no"] = m.group(1)
    return _finish(fields)
