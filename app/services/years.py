"""纪年 → 公历换算：民国 N 年、明清年号 N 年、纯公历年。
员工只管拍，档案里的 year_sort 由这里算出来供筛选排序。
"""
import re
from typing import Optional

# 年号 → 改元当年（第 1 年 = 该值）
ERA_START = {
    "洪武": 1368, "建文": 1399, "永乐": 1403, "洪熙": 1425, "宣德": 1426,
    "正统": 1436, "景泰": 1450, "天顺": 1457, "成化": 1465, "弘治": 1488,
    "正德": 1506, "嘉靖": 1522, "隆庆": 1567, "万历": 1573, "泰昌": 1620,
    "天启": 1621, "崇祯": 1628,
    "顺治": 1644, "康熙": 1662, "雍正": 1723, "乾隆": 1736, "嘉庆": 1796,
    "道光": 1821, "咸丰": 1851, "同治": 1862, "光绪": 1875, "宣统": 1909,
}
_ERA_RE = "|".join(ERA_START)

_UNITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
          "六": 6, "七": 7, "八": 8, "九": 9}
_TENS = {"十": 10, "廿": 20, "卅": 30}


def _cn_to_int(s: str) -> Optional[int]:
    """中文数字转整数（1~99 足够纪年用）：三十六→36、十八→18、元→1。"""
    s = s.strip()
    if s == "元":
        return 1
    if s.isdigit():
        return int(s) if 0 < int(s) < 200 else None
    if len(s) == 1:
        return _UNITS.get(s) or _TENS.get(s)
    m = re.match(r"^([一二三四五六七八九])?(十|廿|卅)([一二三四五六七八九])?$", s)
    if not m:
        return None
    if m.group(2) == "十":
        base = (_UNITS[m.group(1)] if m.group(1) else 1) * 10
    else:  # 廿/卅 固定 20/30，如 廿三=23
        base = _TENS[m.group(2)]
    return base + (_UNITS[m.group(3)] if m.group(3) else 0)


def to_gregorian(text: str) -> Optional[int]:
    """从任意文本里找纪年并换算公历；找不到返回 None。"""
    if not text:
        return None
    m = re.search(r"民国\s*(元|\d{1,3}|[一二三四五六七八九十百]+)\s*年", text)
    if m:
        n = _cn_to_int(m.group(1))
        if n and 1 <= n <= 150:
            return 1911 + n  # 民国元年 = 1912
    m = re.search(rf"({_ERA_RE})\s*(元|\d{{1,3}}|[一二三四五六七八九十廿卅]+)\s*年", text)
    if m:
        n = _cn_to_int(m.group(2))
        start = ERA_START[m.group(1)]
        if n and 1 <= n <= 70:
            return start + n - 1  # 光绪十八年 = 1875 + 18 - 1 = 1892
    m = re.search(r"(?<!\d)(1[89]\d{2}|20[0-4]\d)(?!\d)", text)  # 明确公历 1800-2049（\b 对中文邻接无效）
    if m:
        return int(m.group(1))
    return None
