#!/usr/bin/env bash
# ==========================================
# pdd-auto-reply - 数据库自动备份脚本（backup.sh）
# ------------------------------------------
# 用途：导出 MySQL 全量数据到 ./backups/，按保留天数滚动清理。
#       生产环境建议 crontab 每日执行：
#         10 3 * * * cd /opt/pdd-auto-reply && bash backup.sh >> logs/backup.log 2>&1
# 说明：仅本项目容器内 mysqldump，保留数据卷不动（规范 11）；含敏感凭据，
#       backups/ 目录权限应限制为部署用户（chmod 700）。
# ==========================================
set -euo pipefail

WORK_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKUP_DIR="${WORK_DIR}/backups"
RETAIN_DAYS="${BACKUP_RETAIN_DAYS:-14}"
STAMP="$(date +%Y%m%d_%H%M%S)"

mkdir -p "${BACKUP_DIR}"
chmod 700 "${BACKUP_DIR}" 2>/dev/null || true

if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  echo "错误: 未检测到 Docker Compose" >&2
  exit 1
fi

echo "[backup] 开始导出 pdd_auto_reply @ ${STAMP}"
${DC} -f "${WORK_DIR}/docker-compose.yml" exec -T mysql \
  sh -c 'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --databases pdd_auto_reply --single-transaction --routines' \
  | gzip > "${BACKUP_DIR}/pdd_auto_reply_${STAMP}.sql.gz"

# 可选：连同浏览器会话目录打包（店铺登录态，丢失需人工重新验证码登录）
if [ -d "${WORK_DIR}/browser_data" ]; then
  tar -czf "${BACKUP_DIR}/browser_data_${STAMP}.tar.gz" -C "${WORK_DIR}" browser_data 2>/dev/null || true
fi

# 滚动清理过期备份
find "${BACKUP_DIR}" -name "*.gz" -mtime "+${RETAIN_DAYS}" -delete

echo "[backup] 完成: ${BACKUP_DIR}/pdd_auto_reply_${STAMP}.sql.gz"
