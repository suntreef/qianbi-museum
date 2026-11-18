# 项目协作与评级公司适配台账（多 Agent 共享记忆）

本文件供智谱（ZCode）与 Antigravity 等协作 AI 共同参考，确保跨工具接力时遵守同一套架构原则与实测经验。

## 核心铁律

1. **宁空勿错**：识别不把握的字段一律留空交人工确认卡补齐，绝不硬猜污染档案。
2. **URL 结构化字段优先**：从二维码 URL 提取出的 `cert_no`、`grade_raw`、`grade_scale` 及域名台账判定的 `grade_co`，**绝不允许被标签 OCR 的猜测值覆盖**（实测：条码数字截断误认、纸币冠字号易与评级编号混淆）。
3. **三层容错流水线**（`app/ingest.py`）：
   - ① 二维码解码（`qr.py`，含右上角小码 6~10x 裁切放大）→ URL 结构提取（`rules.parse_cert_url`）→ 证书页抓取（`webfetch.fetch_page`，含 SSRF 重定向逐跳公网校验 + `is_spa_shell` 前端空壳拦截）；
   - ② 页面 403 / 离线 / 前端 SPA 空壳 / 无二维码时，自动走本地 RapidOCR（`ocr.py`，整盒照先裁顶部 42% 标签区防竖排小字与大号分数粘连）+ `rules.parse_label_ocr` 合并；
   - ③ 所有真实样本必须脱敏固化进 `tests/smoke.py` 永久回归测试，每次改动后执行 `python -u tests/smoke.py` 确保零回归（零 `[FAIL]`）。

## 各评级公司真实样本适配进度

| 评级公司 | 状态 | 二维码 / URL 特征 | 证书页实测情况 | 标签 OCR 关键特征与容错 |
|---|---|---|---|---|
| **国标 NSCA** | ✅ 已打通 | `cfcr.org.cn`，`DZB` 字母+数字编号 | 静态文本可读，「藏品编号/名称/年代/面值/冠号/分数」键值格式 | `parse_labeled_fields` 直接提取 |
| **爱藏 ACG** | ✅ 已打通 | 部分标签正面无二维码（官网 `pj.airmb.com`） | 走纯标签 OCR 路径 | `62 EPQ` 常误识为 `62 EPO`；`冠号：1-B755211` 粘连容错 |
| **PMG** | ✅ 已打通 | 右上角极小二维码（需裁切+6~10x 放大），`pmgnotes.com/certlookup/{编号}/{分数}/` | Cloudflare 403 拦截——编号+分数直接取自 URL | `Pick#` 与 `S/M#` 粘连容错；`5 Cents` 英文面值映射；`S/N` 冠号提取 |
| **华夏评级** | ✅ 已打通 | 右侧二维码，`huaxiapj.com/c/{编号}` → 跳转 `querySendItem.html?orderNum={编号}`（另有 `huaxiapj.net`、10 位以上有证链 `z.wpt.la/218/219/{编号}`） | 返回 200 但为 Vue SPA 空壳（`{{QueryResult.xxx}}`），接口超 10 次有滑块验证——已由 `is_spa_shell` 拦截占位图并降级为「URL 取编号 + 标签 OCR」 | `EPQ` 竖排在大号分数右侧，整盒照需顶部 42% 裁切避免 `53+E→535`，排序呈前置 `EPO 53`/`EPC 53`；补齐中文「角/分」面值（避开古钱 `82分`）；猜 `cert_no` 前剔除纸币 `冠号` 防误抓 |
| **保粹 CCGA** | ✅ 已打通 | 右侧二维码，`z.wpt.la/43/198/{编号}` → 跳转 `youzheng.link/detailnew/43/198/{编号}`；背面一维码（`qr.py` 已确保优先返回 `http(s)` 二维码） | 有证链前端空壳（正文仅 `-->`）——`is_spa_shell` 拦截，URL 取混合编号 + 标签 OCR | 编号为字母数字穿插（如 `B3735A3583`）；分数常为裸数字（如 `65` 无 EPQ）；`S/NC398648-Noteguarantee` 粘连提取 |
| **7788 评级** | ✅ 已打通 | 右侧二维码，`m.7788.com/new/api/Grade/productCode/c/{编号}`（背面防伪贴 `m.997788.com/p/grade/`） | 302 跳强制登录页 `Localstoragelogin`（正文 `h5登录`）——保留原二维码 URL 作 `cert_url`，URL 取尾字母编号 + 标签 OCR | 编号带尾字母（如 `70002207725F`）；含结构化 `年代：`、`规格：冠号xxx`、`特征：大东版，章签，单字轨`（自动写入 `variety` 并匹配 `大东书局`）；裸分数 `60` |
| **PCGS** | ✅ 已打通 | PCGS Banknote 无二维码（NFC + 一维码），自动拼 `pcgs.com/banknotecert/{编号}` 供浏览器直达 | Cloudflare 403 拦截——走标签条码行 + OCR 全自动解析 | 盾牌标 `PCGS` 受斜纹干扰易误识为 `PGS`/`RGS`，靠专属条码行 `{品种号}.{分数}/{证书号}`（如 `1147710.45/88052280`）100% 锁定 `PCGS` + 分数 + 编号；省钞目录号 `Pick# S1139a` + `S/M#C103-40a` 粘连容错；`Serial#` 冠号；`10/20/50 Cents` 映射 `壹角/贰角/伍角`；英文无「年」字年份提取 |
| **公博 GBCA** | ⏳ 待测样本 | 预登记 `gongbocoins.com` | 待真实标签实测 | 待实测 |
| **闻德 WDVAC** | ⏳ 待测样本 | 预登记 `wendepj.com` | 待真实标签实测 | 待实测 |
