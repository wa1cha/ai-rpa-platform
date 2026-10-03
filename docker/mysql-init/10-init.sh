#!/bin/bash
# ============================================================
# MySQL 容器首次初始化脚本 —— Phase 7 部署用
#
# 复刻 scripts/init_db.sh 的核心逻辑，在容器里等价地做一遍：
#   ① 建第二个库 mock_erp（第一个库 ai_rpa 由官方镜像按 MYSQL_DATABASE 自动建好）
#   ② 授权给应用账号 —— 镜像只对 MYSQL_DATABASE 授权，mock_erp 得我们自己补
#   ③ 逐文件改写 schema 里硬编码的 `USE \`<库名>\`;`，再载入
#   ④ 载入模拟 ERP 的库存种子
#
# 为什么不能像早先的 compose 那样直接把 database/schema 整个挂进 initdb.d：
#   - 官方 entrypoint 只会自动建 MYSQL_DATABASE 一个库，009_mock_erp.sql 与
#     database/seed/03_erp_inventory.sql 都打向 mock_erp → 直接挂必失败；
#   - schema 文件里的 `USE` 是硬编码的，且 009 属于另一个库，必须按文件分流。
# scripts/init_db.sh 早就用 sed 处理了这两点，这里照搬同一套口径。
#
# 运行时机：mysql 数据目录为空时，entrypoint 按文件名顺序执行本目录下的文件，
# 且此时 MYSQL_DATABASE / MYSQL_USER / MYSQL_ROOT_PASSWORD 等 env 都已就位。
# 数据卷一旦有数据就不会重跑 —— 想重来先 `docker compose down -v`。
# ============================================================
set -euo pipefail

SCHEMA_DIR=/init/schema
SEED_DIR=/init/seed

# 容器内用 socket 连本地 MySQL。root 口令来自 compose 注入的 env。
mysql_root() {
  mysql --protocol=socket -uroot -p"${MYSQL_ROOT_PASSWORD}" "$@"
}

echo "[init] 建库 mock_erp 并授权给应用账号 '${MYSQL_USER}'@'%'"
mysql_root <<SQL
CREATE DATABASE IF NOT EXISTS \`mock_erp\`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL PRIVILEGES ON \`mock_erp\`.* TO '${MYSQL_USER}'@'%';
FLUSH PRIVILEGES;
SQL

echo "[init] 载入表结构（001..008、010 → ${MYSQL_DATABASE}，009 → mock_erp）"
for sql_file in "$SCHEMA_DIR"/0*.sql; do
  base="$(basename "$sql_file")"
  if [[ "$base" == 009_* ]]; then
    target_db="mock_erp"
  else
    target_db="${MYSQL_DATABASE}"
  fi
  echo "[init]   ${base} → ${target_db}"
  sed -E "s/USE \`[a-z_]+\`;/USE \`${target_db}\`;/" "$sql_file" | mysql_root
done

echo "[init] 载入模拟 ERP 种子（*.sql；02 是空占位，库存种子在 03）"
for seed_file in "$SEED_DIR"/*.sql; do
  [[ -e "$seed_file" ]] || continue
  echo "[init]   $(basename "$seed_file")"
  mysql_root < "$seed_file"
done

echo "[init] 完成：已建 ${MYSQL_DATABASE} + mock_erp 并载入表结构"
