"""环境变量配置——全部可在 docker-compose.yml 的 environment 里改。"""
import os
from pathlib import Path

# 数据目录：数据库 + 照片都在这里，备份 = 复制这个目录
DATA_DIR = Path(os.environ.get("DATA_DIR", "/app/data"))
PHOTO_DIR = DATA_DIR / "photos"
BATCH_DIR = DATA_DIR / "batch"
DB_PATH = DATA_DIR / "museum.db"

# 登录
APP_PASSWORD = os.environ.get("APP_PASSWORD", "changeme")
SESSION_SECRET = os.environ.get("SESSION_SECRET", APP_PASSWORD + "-session-secret")

# 大模型（可选插口，OpenAI 兼容协议）：不填 Key 时全部走免费本地路径，
# 填了自动启用增强功能（难解析证书页兜底、OCR 提质、裸币识别）。
# 0 元方案：智谱免费档（glm-4v-flash 等）或硅基流动免费模型，任选其一填入。
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
LLM_MODEL = os.environ.get("LLM_MODEL", "glm-4.5v")

# 证书页抓取限频（秒/次）
FETCH_INTERVAL_SECONDS = float(os.environ.get("FETCH_INTERVAL_SECONDS", "4"))

# 藏品编号前缀，如 M-2026-0001
ITEM_CODE_PREFIX = os.environ.get("ITEM_CODE_PREFIX", "M")

# 游客导览页口令：留空 = 内网直接可看（展柜扫码场景）；设了值则需 ?t=口令
PUB_TOKEN = os.environ.get("PUB_TOKEN", "")

# 部署了 HTTPS（如经反向代理）时设为 1，Cookie 加 Secure 标记
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "") == "1"

# 静态资源目录（随代码打包，不在数据盘）
STATIC_DIR = Path(__file__).parent / "static"

for _d in (DATA_DIR, PHOTO_DIR, BATCH_DIR):
    _d.mkdir(parents=True, exist_ok=True)
