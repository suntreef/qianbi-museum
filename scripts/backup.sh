#!/bin/sh
# NAS 计划任务备份脚本：数据库 + 全部照片 打包到备份目录
# 用法（群晖：控制面板 → 任务计划 → 新增 → 计划任务 → 用户定义的脚本）：
#   /volume1/docker/qianbi/scripts/backup.sh
# 建议每天凌晨执行；BACKUP_DIR 建议指向第二块盘或挂载的异地目录。

# ===== 按你的实际路径修改这两行 =====
APP_DIR="/volume1/docker/qianbi"        # 项目目录（含 data/）
BACKUP_DIR="/volume1/backup/qianbi"     # 备份输出目录
# ===================================

STAMP=$(date +%Y%m%d_%H%M)
mkdir -p "$BACKUP_DIR"

# 1) 数据库：用 sqlite3 backup 命令保证一致性（NAS 一般自带；没有的话用下面的 cp 代替）
if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$APP_DIR/data/museum.db" ".backup '$BACKUP_DIR/museum_$STAMP.db'"
else
    cp "$APP_DIR/data/museum.db" "$BACKUP_DIR/museum_$STAMP.db"
fi

# 2) 照片：增量同步（保留删除镜像可加 --delete，按需）
if command -v rsync >/dev/null 2>&1; then
    rsync -a "$APP_DIR/data/photos/" "$BACKUP_DIR/photos/"
else
    cp -r "$APP_DIR/data/photos" "$BACKUP_DIR/photos_$STAMP"
fi

# 3) 只保留最近 30 份数据库备份，防止占满盘
ls -1t "$BACKUP_DIR"/museum_*.db 2>/dev/null | tail -n +31 | xargs -r rm -f

echo "备份完成：$BACKUP_DIR"
