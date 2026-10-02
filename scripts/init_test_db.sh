#!/usr/bin/env bash
#
# 创建测试库 ai_rpa_test 并载入表结构（只需执行一次）
#
# 用法：
#   ./scripts/init_test_db.sh
#
# 为什么单独建一个库，而不是在 ai_rpa 上跑测试：
#   集成测试的前置动作是 TRUNCATE 所有表。跑在开发库上等于每次跑测试都把
#   本地数据清空一次 —— 这种「一不小心就毁了手头数据」的设计，迟早会有人中招。
#   测试库与开发库物理隔离后，TRUNCATE 的爆炸半径被限制在测试库内。
#
# 与 init_db.sh 的分工：
#   init_db.sh       建 ai_rpa + mock_erp，日常开发用
#   本脚本           建 ai_rpa_test + mock_erp_test，只跑测试用
#
# 两个测试库都建：主平台的集成测试用 ai_rpa_test，模拟 ERP 自己的测试用
# mock_erp_test（它的 conftest 会清空 erp_* 三张表）。两套测试跑在同一个 MySQL
# 实例上但用不同的库，互不干扰 —— 就像两个应用本身一样。
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
SCHEMA_DIR="$ROOT_DIR/database/schema"

#: 测试库名。必须与 tests/conftest.py 里的 TEST_MYSQL_DB 一致 ——
#: 两处不一致会表现为「测试连上了开发库」，而 conftest 的第一层保护
#: 会在收集阶段就把这种情况拦下来（报错而不是静默清空数据）。
TEST_DB="ai_rpa_test"

#: 模拟 ERP 的测试库。必须与 mock/erp/tests/conftest.py 里的 TEST_ERP_DB 一致，
#: 理由同上。
TEST_ERP_DB="mock_erp_test"

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

for var in MYSQL_HOST MYSQL_PORT MYSQL_USER MYSQL_PASSWORD; do
  if [[ -z "${!var}" ]]; then
    echo "错误：.env 里缺少 $var" >&2
    exit 1
  fi
done

cat <<INFO
即将执行：
  创建数据库   : ${TEST_DB}、${TEST_ERP_DB}（已存在则跳过）
  授权         : ${MYSQL_USER} 拥有这两个库的全部权限
  载入表结构   : 001..008 → ${TEST_DB}
                 009_mock_erp → ${TEST_ERP_DB}
  MySQL 地址   : ${MYSQL_HOST}:${MYSQL_PORT}

  注意：这两个库会被测试脚本反复清空，不要往里放任何需要保留的数据。
INFO

read -r -p "确认继续？[y/N] " reply
if [[ ! "$reply" =~ ^[Yy]$ ]]; then
  echo "已取消"
  exit 0
fi

# root 认证。两种方式，挑一种：
#   · 预先设好环境变量 MYSQL_ROOT_PASSWORD（可以是空串 = 无密码）→ 完全不交互，
#     CI 和没有 TTY 的场合靠这条。
#   · 不设 → 隐藏式提示输入，体验与 init_db.sh 一致。
#
# 两条路最终都经 MYSQL_PWD 传给客户端，**不用 -p**：
# 命令行参数会出现在 `ps` 里，同机其他用户能看到密码。
# 用 ${VAR+set} 判断「是否已设置」而不是「是否非空」—— 本地 brew 装的
# MySQL root 往往就是空密码，把「设为空串」和「没设置」区分开很重要。
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
CREATE DATABASE IF NOT EXISTS \`${TEST_DB}\`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE DATABASE IF NOT EXISTS \`${TEST_ERP_DB}\`
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

GRANT ALL PRIVILEGES ON \`${TEST_DB}\`.* TO '${MYSQL_USER}'@'localhost';
GRANT ALL PRIVILEGES ON \`${TEST_DB}\`.* TO '${MYSQL_USER}'@'127.0.0.1';
GRANT ALL PRIVILEGES ON \`${TEST_ERP_DB}\`.* TO '${MYSQL_USER}'@'localhost';
GRANT ALL PRIVILEGES ON \`${TEST_ERP_DB}\`.* TO '${MYSQL_USER}'@'127.0.0.1';

FLUSH PRIVILEGES;
SQL

# ---------- ② 载入表结构（用应用账号即可，它已有该库的全部权限）----------
#
# 每个 schema 文件里都硬编码了 `USE \`<库名>\`;`，直接用会把表建到开发库上。
# 所以逐文件把这一行改写成测试库名再喂给 mysql —— 改写的是**流**，不动磁盘上的文件。
#
# 匹配的是 `USE \`<任意小写字母/下划线>\`;` 而不是具体库名：001..008 写的是
# `ai_rpa`，009 写的是 `mock_erp`，两套库名都要能改掉。
#
# 为什么用 MYSQL_PWD 而不是 -p"$MYSQL_PASSWORD"：
# 命令行参数会出现在 `ps` 输出里，同一台机器上的其他用户能看到密码。
echo "载入表结构..."
for sql_file in "$SCHEMA_DIR"/0*.sql; do
  base="$(basename "$sql_file")"

  # 009 是 mock_erp 的建表，属于另一个库 —— 换一个测试库名，照常载入。
  if [[ "$base" == 009_* ]]; then
    target_db="$TEST_ERP_DB"
  else
    target_db="$TEST_DB"
  fi

  echo "  载入 ${base} → ${target_db}"
  sed -E "s/USE \`[a-z_]+\`;/USE \`${target_db}\`;/" "$sql_file" \
    | MYSQL_PWD="$MYSQL_PASSWORD" mysql -h "$MYSQL_HOST" -P "$MYSQL_PORT" -u "$MYSQL_USER"
done

echo
echo "完成。验证："
echo "  MYSQL_PWD=<密码> mysql -h $MYSQL_HOST -P $MYSQL_PORT -u$MYSQL_USER -e 'USE $TEST_DB; SHOW TABLES;'"
echo "  MYSQL_PWD=<密码> mysql -h $MYSQL_HOST -P $MYSQL_PORT -u$MYSQL_USER -e 'USE $TEST_ERP_DB; SHOW TABLES;'"
echo
echo "接着跑测试（两套测试各有各的 pytest.ini，要在各自的目录下跑）："
echo "  /opt/anaconda3/envs/ai-rpa/bin/python -m pytest -m unit          # 不需要 MySQL/Redis"
echo "  /opt/anaconda3/envs/ai-rpa/bin/python -m pytest                  # 主平台全部"
echo "  cd mock/erp && /opt/anaconda3/envs/ai-rpa/bin/python -m pytest   # 模拟 ERP 全部"
