"""字典初始数据：品类、朝代、时代系列、纸币机构/券种/印刷厂等。

元组格式：(type, name, aliases, sort, code)
code 仅品类/朝代/时代系列使用——馆内编号体系「品类码-时代码-年-流水」取自这里，
后台可改 code（改完只影响新编号，老编号不变）。
aliases 用于两件事：① 搜索匹配 ② 评级标签英文自动映射中文。
种子只做"新增不覆盖"（名称冲突时仅同步 code），后台改过的条目不会被覆盖。
"""

SEEDS = [
    # (type, name, aliases, sort, code)
    # —— 品类
    ("category", "纸币", "", 1, "ZB"),
    ("category", "银元", "", 2, "YY"),
    ("category", "铜钱", "", 3, "TQ"),
    ("category", "镍铝币", "", 4, "NL"),
    ("category", "金银币", "", 5, "JY"),
    ("category", "契约票证", "地契, 当票, 文书, 票证", 6, "QP"),
    ("category", "其他", "", 99, "QT"),

    # —— 朝代（有序，古钱筛选/排序用）
    ("dynasty", "先秦", "", 1, "XQ"),
    ("dynasty", "秦", "", 2, "QN"),
    ("dynasty", "汉", "", 3, "HN"),
    ("dynasty", "三国两晋南北朝", "", 4, "SG"),
    ("dynasty", "隋", "", 5, "SUI"),
    ("dynasty", "唐", "", 6, "TG"),
    ("dynasty", "五代十国", "", 7, "WD"),
    ("dynasty", "宋", "", 8, "SG2"),
    ("dynasty", "辽金西夏", "", 9, "LJ"),
    ("dynasty", "元", "", 10, "YU"),
    ("dynasty", "明", "", 11, "MI"),
    ("dynasty", "清", "", 12, "QC"),
    ("dynasty", "民国", "", 13, "MG"),
    ("dynasty", "现代", "", 14, "XD"),

    # —— 时代系列（纸币的"大时代"轴）
    ("series", "明清票券", "银票, 宝钞, 官票", 1, "MQ"),
    ("series", "民国纸币", "民国钞票", 2, "MG"),
    ("series", "边区·解放区", "边区票, 边币, 抗币, 解放区", 3, "BQ"),
    ("series", "第一套人民币", "老人民币, 旧版人民币", 4, "R1"),
    ("series", "第二套人民币", "", 5, "R2"),
    ("series", "第三套人民币", "", 6, "R3"),
    ("series", "第四套人民币", "", 7, "R4"),
    ("series", "第五套人民币", "", 8, "R5"),
    ("series", "纪念钞", "", 9, "JN"),
    ("series", "外国纸币", "外钞", 98, "WG"),
    ("series", "其他", "", 99, "OT"),

    # —— 发行机构：明清官方
    ("bank", "户部", "户部官票, 大清宝钞", 100, ""),
    ("bank", "户部银行", "大清户部银行", 101, ""),
    ("bank", "大清银行", "度支部, Great Qing Bank", 102, ""),

    # —— 发行机构：民国国家行局
    ("bank", "中央银行", "Central Bank of China", 1, ""),
    ("bank", "中国银行", "Bank of China", 2, ""),
    ("bank", "交通银行", "Bank of Communications", 3, ""),
    ("bank", "中国农民银行", "Farmers Bank of China", 4, ""),
    ("bank", "中央储备银行", "Central Reserve Bank of China", 5, ""),

    # —— 发行机构：伪政权银行
    ("bank", "中国联合准备银行", "Federal Reserve Bank of China", 6, ""),
    ("bank", "满洲中央银行", "Manchukuo Central Bank", 7, ""),
    ("bank", "蒙疆银行", "Bank of Mengchiang", 8, ""),
    ("bank", "冀东银行", "", 9, ""),
    ("bank", "台湾银行", "Bank of Taiwan", 10, ""),

    # —— 发行机构：商业银行（多已歇业——"消失的银行"）
    ("bank", "中国通商银行", "Commercial Bank of China, 中国第一家银行", 20, ""),
    ("bank", "浙江兴业银行", "", 21, ""),
    ("bank", "四明银行", "Ningpo Commercial Bank", 22, ""),
    ("bank", "中国实业银行", "Industrial Bank of China", 23, ""),
    ("bank", "中国国货银行", "National Commercial and Savings Bank", 24, ""),
    ("bank", "中南银行", "China & South Sea Bank", 25, ""),
    ("bank", "盐业银行", "Yien Yieh Commercial Bank", 26, ""),
    ("bank", "金城银行", "Kincheng Banking Corporation", 27, ""),
    ("bank", "大陆银行", "Continental Bank", 28, ""),
    ("bank", "中国垦业银行", "China Agricultural and Industrial Bank", 29, ""),
    ("bank", "农商银行", "", 30, ""),
    ("bank", "北洋保商银行", "", 31, ""),

    # —— 发行机构：省银行与官银号
    ("bank", "东三省官银号", "", 40, ""),
    ("bank", "四川省银行", "", 41, ""),
    ("bank", "湖南省银行", "", 42, ""),
    ("bank", "陕西省银行", "", 43, ""),
    ("bank", "富滇银行", "", 44, ""),
    ("bank", "新疆省银行", "", 45, ""),
    ("bank", "广西银行", "", 46, ""),

    # —— 发行机构：边区·解放区银行
    ("bank", "陕甘宁边区银行", "Shensi-Kansu-Ninghsia Border Region Bank", 60, ""),
    ("bank", "晋察冀边区银行", "晋察冀边区", 61, ""),
    ("bank", "冀南银行", "", 62, ""),
    ("bank", "北海银行", "Bank of Peihai", 63, ""),
    ("bank", "西北农民银行", "", 64, ""),
    ("bank", "中州农民银行", "", 65, ""),
    ("bank", "东北银行", "", 66, ""),
    ("bank", "关东银行", "", 67, ""),
    ("bank", "内蒙古人民银行", "Inner Mongolian People's Bank", 68, ""),
    ("bank", "华中银行", "华中", 69, ""),
    ("bank", "江淮银行", "", 70, ""),
    ("bank", "盐阜银行", "", 71, ""),
    ("bank", "淮南银行", "", 72, ""),
    ("bank", "江南银行", "", 73, ""),
    ("bank", "浙东银行", "", 74, ""),
    ("bank", "豫鄂边区建设银行", "", 75, ""),
    ("bank", "热河省银行", "", 76, ""),
    ("bank", "长城银行", "Great Wall Bank", 77, ""),
    ("bank", "鲁西银行", "", 78, ""),
    ("bank", "光华商店", "光华商店代价券", 79, ""),

    # —— 发行机构：外商银行
    ("bank", "汇丰银行", "Hongkong and Shanghai Banking Corporation, HSBC", 50, ""),
    ("bank", "麦加利银行", "Chartered Bank, 渣打银行", 51, ""),
    ("bank", "花旗银行", "National City Bank of New York", 52, ""),
    ("bank", "东方汇理银行", "Banque de l'Indochine", 53, ""),

    # —— 发行机构：现代
    ("bank", "中国人民银行", "People's Bank of China, PBOC", 200, ""),

    # —— 券种（按时代大致排序）
    ("note_type", "宝钞", "大明通行宝钞, 大明宝钞, 大清宝钞", 1, ""),
    ("note_type", "官票", "户部官票, 银票, 银两票", 2, ""),
    ("note_type", "钱票", "银钱票, 钱帖, 私票", 3, ""),
    ("note_type", "法币", "法币券", 4, ""),
    ("note_type", "金圆券", "Gold Yuan, 金元券", 5, ""),
    ("note_type", "银元券", "Silver Yuan, 银元票", 6, ""),
    ("note_type", "关金券", "关金, Customs Gold Unit, CGU, 海关金单位兑换券", 7, ""),
    ("note_type", "国币券", "", 8, ""),
    ("note_type", "兑换券", "", 9, ""),
    ("note_type", "边币", "边区币, 农币", 10, ""),
    ("note_type", "辅币券", "角券, 分券", 11, ""),
    ("note_type", "军用票", "军用钞票", 12, ""),
    ("note_type", "人民币", "RMB, 人民币纸币", 13, ""),
    ("note_type", "其他", "", 99, ""),

    # —— 文书类型（品类「契约票证」用；立契人/当铺字号等细节放备注）
    ("doc_type", "地契", "红契, 白契, 官契, 田契", 1, ""),
    ("doc_type", "房契", "铺面房契", 2, ""),
    ("doc_type", "当票", "当铺票据, 当铺执照, 死当活当", 3, ""),
    ("doc_type", "婚书", "庚帖, 婚约", 4, ""),
    ("doc_type", "老股票", "股据, 商号股票", 5, ""),
    ("doc_type", "公债券", "公债票, 救国公债, 国库券", 6, ""),
    ("doc_type", "钱庄票", "银号票, 汇票, 庄票", 7, ""),
    ("doc_type", "粮票", "粮券", 8, ""),
    ("doc_type", "布票", "", 9, ""),
    ("doc_type", "执照", "牙帖, 营业执照, 许可证", 10, ""),
    ("doc_type", "告示", "布告", 11, ""),
    ("doc_type", "其他", "", 99, ""),

    # —— 印刷厂
    ("printer", "美国钞票公司", "American Bank Note Company, ABNC", 1, ""),
    ("printer", "英国华德路公司", "Waterlow & Sons, 华德路", 2, ""),
    ("printer", "英国德纳罗公司", "De La Rue, Thomas De La Rue", 3, ""),
    ("printer", "英国布拉德伯里威尔金森公司", "Bradbury Wilkinson & Co, BWC", 4, ""),
    ("printer", "日本凸版印刷", "Toppan Printing, 凸版印刷株式会社", 5, ""),
    ("printer", "财政部印刷局", "北京白纸坊, 度支部印刷局", 10, ""),
    ("printer", "中央印制厂", "", 11, ""),
    ("printer", "中华书局", "Chung Hwa Book Co", 12, ""),
    ("printer", "商务印书馆", "Commercial Press", 13, ""),
    ("printer", "大东书局", "Tai Tung Press", 14, ""),
    ("printer", "大业印刷公司", "Ta Yeh Printing", 15, ""),
    ("printer", "京华印书局", "", 16, ""),

    # —— 评级公司
    ("grade_co", "PMG", "Paper Money Guaranty", 1, ""),
    ("grade_co", "公博", "GBCA, 公博评级", 2, ""),
    ("grade_co", "爱藏", "", 3, ""),
    ("grade_co", "华夏", "CHGX, 华夏评级", 4, ""),
    ("grade_co", "闻德", "WDVAC, 闻德评级", 5, ""),
    ("grade_co", "保粹", "TSR, 保粹评级", 6, ""),
    ("grade_co", "PCGS", "", 7, ""),
    ("grade_co", "NGC", "", 8, ""),
    ("grade_co", "ASG", "", 9, ""),
    ("grade_co", "国标", "NSCA, 国标文物评级, 国标文物评级检测认证中心, CFCR", 10, ""),
    ("grade_co", "无", "", 99, ""),

    # —— 藏品等级（分级：展览遴选/保险/盘点重点）
    ("item_level", "镇馆之宝", "", 1, ""),
    ("item_level", "一级", "", 2, ""),
    ("item_level", "二级", "", 3, ""),
    ("item_level", "三级", "", 4, ""),
    ("item_level", "一般", "", 5, ""),

    # —— 保存状况（定期检查记录）
    ("condition_st", "完好", "", 1, ""),
    ("condition_st", "轻微氧化", "", 2, ""),
    ("condition_st", "需处理", "", 3, ""),
    ("condition_st", "已修复", "", 4, ""),

    # —— 材质
    ("material", "纸", "", 1, ""),
    ("material", "银", "", 2, ""),
    ("material", "铜", "", 3, ""),
    ("material", "铁", "", 4, ""),
    ("material", "镍", "", 5, ""),
    ("material", "铝", "", 6, ""),
    ("material", "金", "", 7, ""),
    ("material", "其他", "", 99, ""),
]


def load_seeds() -> int:
    """写入库：名称已存在时只同步 code（后台改过的名称/别名/排序不动）。"""
    from ..models import get_conn

    changed = 0
    with get_conn() as conn:
        for type_, name, aliases, sort, code in SEEDS:
            cur = conn.execute(
                """INSERT INTO dict_entry(type, name, aliases, sort, code) VALUES(?,?,?,?,?)
                   ON CONFLICT(type, name) DO UPDATE SET code = excluded.code""",
                (type_, name, aliases or None, sort, (code or "").strip() or None))
            changed += cur.rowcount
    return changed
