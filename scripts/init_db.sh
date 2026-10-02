#!/usr/bin/env bash
#
# 创建数据库、应用账号，并载入表结构（只需执行一次）
#
# 用法：
#   ./scripts/init_db.sh
#
# 说明：
#   - 账号和密码从 .env 读取，脚本里不写任何密钥
#   - root 认证：预设 MYSQL_ROOT_PASSWORD 环境变量则完全不交互（CI / 无 TTY 用）；
#     否则隐藏式提示输入（本机 brew 的 MySQL root 往往无密码，直接回车）
#   - 创建两个库：主库 ai_rpa，模拟 ERP 库 mock_erp
#   - 载入 database/schema/0*.sql 建表：001..008 → 主库，009 → mock_erp
#
# 与 init_test_db.sh 的分工：
#   本脚本           建 ai_rpa + mock_erp，日常开发用
#   init_test_db.sh  建 ai_rpa_test + mock_erp_test，只跑测试用
#
# 表结构以 database/schema/ 下的 001..009 为准（它们已包含历次迁移的最终形态，
# 例如 005 已带 cancel_reason、009 已带 ERP 来源单号唯一键）。database/migrations/
# 里的 010/011 是给「已经建过表的旧库」做增量升级用的，全新初始化不需要再跑。
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
SCHEMA_DIR="$ROOT_DIR/database/schema"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "错误：找不到 $ENV_FILE" >&2
  echo "请先执行：cp .env.example .env" >&2
  exit 1
fi

# 只取需要的键，避免把 .env 当脚本执行
value_of() {
  grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-
}

MYSQL_HOST="$(value_of MYSQL_HOST)"
MYSQL_PORT="$(value_of MYSQL_PORT)"
MYSQL_USER="$(value_of MYSQL_USER)"
MYSQL_PASSWORD="$(value_of MYSQL_PASSWORD)"
MYSQL_DB="$(value_of MYSQL_DB)"

for var in MYSQL_HOST MYSQL_PORT MYSQL_USER MYSQL_PASSWORD MYSQL_DB; do
  if [[ -z "${!var}" ]]; then
    echo "错误：.env 里缺少 $var" >&2
    exit 1
  fi
done

cat <<INFO
即将执行：
  创建数据库   : ${MYSQL_DB} 和 mock_erp
  创建账号     : ${MYSQL_USER} (密码取自 .env)
  授权范围     : 仅这两个库
  载入表结构   : 001..008 → ${MYSQL_DB}
                 009_mock_erp → mock_erp
  MySQL 地址   : ${MYSQL_HOST}:${MYSQL_PORT}

注意：建表用的是 CREATE TABLE IF NOT EXISTS，重复执行不会破坏已有数据，
      但也不会帮你升级已存在的旧表结构。
INFO

read -r -p "确认继续？[y/N] " reply
if [[ ! "$reply" =~ ^[Yy]$ ]]; then
  echo "已取消"
  exit 0
fi

# root 认证。两种方式，挑一种：
#   · 预先设好环境变量 MYSQL_ROOT_PASSWORD（可以是空串 = 无密码）→ 完全不交互
#   · 不设 → 隐藏式提示输入
# 两条路都经 MYSQL_PWD 传给客户端，**不用 -p**：命令行参数会出现在 `ps` 里。
# 用 ${VAR+set} 判断「是否已设置」而不是「是否非空」—— 本地 brew 装的 MySQL
# root 往往就是空密码，要把「设为空串」和「没设置」区分开。
# （也不用 bash 4.2+ 的 -v，macOS 自带的还是 bash 3.2。）
if [[ -n "${MYSQL_ROOT_PASSWORD+set}" ]]; then
  echo "root 认证：读环境变量 MYSQL_ROOT_PASSWORD"
else
  read -r -s -p "MySQL root 密码（无密码直接回车）: " MYSQL_ROOT_PASSWORD
  echo
fi
export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"

# ---------- ① 建库 + 授权（需要 root）----------
mysql -h "$MYSQL_HOST" -P "$MYSQL_PORT" -uroot <<SQL
CREATE DATABASE IF NOT EXISTS \`${MYSQL_DB}\`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE DATABASE IF NOT EXISTS \`mock_erp\`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS '${MYSQL_USER}'@'localhost' IDENTIFIED BY '${MYSQL_PASSWORD}';
CREATE USER IF NOT EXISTS '${MYSQL_USER}'@'127.0.0.1' IDENTIFIED BY '${MYSQL_PASSWORD}';

GRANT ALL PRIVILEGES ON \`${MYSQL_DB}\`.* TO '${MYSQL_USER}'@'localhost';
GRANT ALL PRIVILEGES ON \`${MYSQL_DB}\`.* TO '${MYSQL_USER}'@'127.0.0.1';
GRANT ALL PRIVILEGES ON \`mock_erp\`.*  TO '${MYSQL_USER}'@'localhost';
GRANT ALL PRIVILEGES ON \`mock_erp\`.*  TO '${MYSQL_USER}'@'127.0.0.1';

FLUSH PRIVILEGES;
SQL

# ---------- ② 载入表结构（用应用账号即可，它已有该库的全部权限）----------
#
# 每个 schema 文件里都硬编码了 `USE \`<库名>\`;`（001..008 写 ai_rpa，009 写
# mock_erp）。逐文件把这一行改写成目标库名再喂给 mysql —— 改写的是**流**，
# 不动磁盘上的文件。
#
# 匹配 `USE \`<任意小写字母/下划线>\`;` 而不是具体库名，这样两套库名都能改掉。
echo "载入表结构..."
for sql_file in "$SCHEMA_DIR"/0*.sql; do
  base="$(basename "$sql_file")"

  # 009 是 mock_erp 的建表，属于另一个库 —— 换成 mock_erp，照常载入。
  if [[ "$base" == 009_* ]]; then
    target_db="mock_erp"
  else
    target_db="$MYSQL_DB"
  fi

  echo "  载入 ${base} → ${target_db}"
  # MYSQL_PWD 已在上面导出给 root；这里用应用账号自己的密码覆盖它。
  sed -E "s/USE \`[a-z_]+\`;/USE \`${target_db}\`;/" "$sql_file" \
    | MYSQL_PWD="$MYSQL_PASSWORD" mysql -h "$MYSQL_HOST" -P "$MYSQL_PORT" -u "$MYSQL_USER"
done

echo
echo "完成。可用以下命令验证："
echo "  MYSQL_PWD=<密码> mysql -h $MYSQL_HOST -P $MYSQL_PORT -u$MYSQL_USER -e 'USE ${MYSQL_DB}; SHOW TABLES;'"
echo "  MYSQL_PWD=<密码> mysql -h $MYSQL_HOST -P $MYSQL_PORT -u$MYSQL_USER -e 'USE mock_erp; SHOW TABLES;'"
