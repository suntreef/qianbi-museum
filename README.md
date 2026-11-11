# 钱币博物馆藏品入库系统

拍照 → 自动识别评级信息 → 人工确认 → 一物一档。
面向民间博物馆的本地化藏品管理系统，全部数据留在自己的 NAS 上。

## 部署（NAS · Docker）

**方式一：拷贝文件夹（最简单，不需要 NAS 上装 git）**

1. 把整个 `qianbi-museum` 文件夹拷到 NAS（File Station 拖拽 / SMB 共享）
2. 编辑 `docker-compose.yml`：`APP_PASSWORD` 必改；`LLM_API_KEY` 可选
3. 打开 **Container Manager → 项目 → 新增**，指向该文件夹，构建启动
   （或 SSH 进入该目录执行 `docker compose up -d --build`）

**方式二：git 拉取（便于以后更新）**

```bash
git clone https://github.com/suntreef/qianbi-museum.git
cd qianbi-museum
# 编辑 docker-compose.yml 改密码
docker compose up -d --build
```

以后更新：`git pull && docker compose up -d --build`

启动后手机/电脑浏览器访问 `http://NAS的IP:8300`，先看 `/api/health` 确认服务正常。

## 备份（重要）

`data/` 目录 = SQLite 数据库 + 全部照片，**整个系统的全部家当**。

- 建议 NAS 计划任务：每晚把 `data/` 打包复制到第二块盘或异地
- 提醒：RAID 不是备份，务必有第二份

## 环境要求

- NAS 可访问外网（抓评级公司证书页 + 调大模型 API）
- 手机与 NAS 同一局域网；远程录入可加 Tailscale 等组网工具
- （可选）大模型 API Key：智谱免费档（GLM-4V-Flash 系列，0 元）或硅基流动免费模型，
  任意 OpenAI 兼容接口均可；不填也完全能用

## 成本说明

| 功能 | 花钱吗 | 说明 |
|---|---|---|
| 评级币录入（拍标签→二维码→证书页→照片下载） | **0 元** | 全部本地完成 |
| 标签 OCR | **0 元** | RapidOCR 本地运行，不调外部 API |
| 裸币录入 | **0 元** | 图鉴下拉 + 手录 + 本库相似提示 |
| 证书页解析（常用公司） | **0 元** | 内置规则适配器，解析不全走半自动确认界面 |
| 大模型增强（难解析页面兜底、裸币 AI 识别） | 可选 | 插口默认关闭；填智谱免费档 Key 可 0 元启用（有限速，批量任务夜间慢跑） |

## 项目结构

```
qianbi-museum/
├── docker-compose.yml      # NAS 部署编排
├── Dockerfile
├── requirements.txt
├── app/
│   ├── main.py             # FastAPI 入口
│   ├── config.py           # 环境变量配置
│   ├── models.py           # SQLite 建表
│   ├── seeds/              # 字典初始数据（机构/券种/印刷厂/朝代…）
│   ├── ingest.py           # 识别流水线（待开发）
│   ├── adapters/           # 证书抓取适配器（待开发）
│   ├── services/           # 二维码/大模型/查重/导出（待开发）
│   └── web/                # 采集页 + 管理后台（待开发）
└── data/                   # 运行时生成：数据库 + 照片（备份这个目录）
```

## 开发状态（v0.1.0，冒烟测试 44/44 通过）

- [x] 项目骨架 / Docker 打包
- [x] 字典系统（机构/券种/系列/印刷厂/文书类型/朝代等 146 条种子 + 管理页）
- [x] 识别流水线（二维码解码 → 证书页抓取 → 字段标签/字典/正则三层解析 → 草稿 → 编号判重）
- [x] 采集页（员工模式：拍标签 → 确认卡 → 拍正反面 → 确认入库；连拍批量、手动录入、照片查重提示）
- [x] 检索（搜索 + 品类/系列/状态/年份区间筛选 + 统计栏）
- [x] 一物一档编辑页（字段修改、状态流转、照片增删、档案删除）
- [x] 补拍任务清单（缺正面/背面档案图的藏品）
- [x] Excel 台账导出（30 列，备案/保险可用）
- [x] 已实测评级公司：国标 NSCA（cfcr.org.cn，真实照片端到端通过）
- [ ] 逐公司精调（PMG/公博/爱藏/华夏/闻德…样本持续收集中）
- [ ] 库位/盘点/送评流转（P2）
