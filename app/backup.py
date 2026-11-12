"""每日自动备份数据库（容器内兜底）。

NAS 计划任务脚本（scripts/backup.sh）是主力备份；
这里是保底：哪怕用户忘了配置计划任务，系统每天也会把数据库
一致性快照存到 data/backups/（保留最近 30 份）。照片仍依赖脚本或手动。
"""
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from .config import DATA_DIR, DB_PATH

KEEP = 30


def run_backup_now() -> Optional[str]:
    """立即做一次一致性备份，返回备份文件路径；失败返回 None。"""
    try:
        bdir = DATA_DIR / "backups"
        bdir.mkdir(parents=True, exist_ok=True)
        dst = bdir / f"museum_{datetime.now():%Y%m%d_%H%M}.db"
        src = sqlite3.connect(str(DB_PATH))
        dest = sqlite3.connect(str(dst))
        try:
            src.backup(dest)
        finally:
            dest.close()
            src.close()
        # 只保留最近 KEEP 份
        files = sorted(bdir.glob("museum_*.db"))
        for f in files[:-KEEP]:
            f.unlink(missing_ok=True)
        return str(dst)
    except Exception:
        return None


def _daily() -> None:
    time.sleep(90)   # 启动稳一稳再第一次备份
    while True:
        run_backup_now()
        time.sleep(24 * 3600)


def start_daily() -> None:
    threading.Thread(target=_daily, daemon=True, name="daily-backup").start()
