#!/usr/bin/env bash
#
# 后台启动模拟 ERP
#
# 用法：
#   ./scripts/start_mock_erp.sh              后台启动（日志写 logs/mock_erp.log）
#   ./scripts/start_mock_erp.sh --foreground 前台启动，Ctrl-C 停（调试用）
#
# 说明：
#   - 解释器从 .env 的 PYTHON_BIN 读，脚本里不写死任何一台机器的路径
#   - 监听地址从应用配置读，不在这里重复一遍默认值
#   - **cwd 必须是 mock/erp**：那个应用里也有一个叫 `app` 的包，
#     和主服务的 backend/app 同名。它靠「自己所在的目录在 sys.path 最前」
#     来保证 import 到自己那一份，所以这里 cd 过去、PYTHONPATH 只给 `.`。
#   - 已在运行时拒绝重复启动，避免两个进程抢同一个端口
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
PID_FILE="$ROOT_DIR/logs/mock_erp.pid"
LOG_FILE="$ROOT_DIR/logs/mock_erp.log"
APP_DIR="$ROOT_DIR/mock/erp"

die() { echo "错误：$*" >&2; exit 1; }

# ---------- 解释器 ----------
[[ -f "$ENV_FILE" ]] || die "找不到 $ENV_FILE，请先执行：cp .env.example .env"

# 只取需要的键，避免把 .env 当脚本执行
value_of() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }

PYTHON_BIN="$(value_of PYTHON_BIN)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
[[ -x "$PYTHON_BIN" ]] || die "解释器不存在或不可执行：$PYTHON_BIN
  在 .env 里把 PYTHON_BIN 改成装了依赖的那个绝对路径。"

# ---------- 监听地址：问应用自己要 ----------
# 和 start.sh 同一个理由：不在这里写默认值，否则 .env 改了端口、脚本还按旧值起。
# 先 cd 再问，因为这条命令依赖「import app 解析到模拟 ERP 那份」。
cd "$APP_DIR"
IFS=' ' read -r ERP_HOST ERP_PORT DEBUG < <(
  PYTHONPATH=. "$PYTHON_BIN" -c \
    'from app.config import settings; print(settings.mock_erp_host, settings.mock_erp_port, settings.debug)'
) || die "读应用配置失败，先单独跑一次上面的 python 看报什么错"

RELOAD_ARGS=()
[[ "$DEBUG" == "True" ]] && RELOAD_ARGS=(--reload)

# ---------- 前台 ----------
if [[ "${1:-}" == "--foreground" ]]; then
  echo "前台启动模拟 ERP http://$ERP_HOST:$ERP_PORT （Ctrl-C 停止）"
  exec "$PYTHON_BIN" -m uvicorn app.main:app \
    --host "$ERP_HOST" --port "$ERP_PORT" ${RELOAD_ARGS[@]+"${RELOAD_ARGS[@]}"}
fi

# ---------- 已经在跑？ ----------
if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE")"
  if kill -0 "$OLD_PID" 2>/dev/null; then
    # ${OLD_PID} 必须带花括号：紧跟其后的全角「）」在 bash 眼里算标识符字符，
    # 不括起来会被吃进变量名，`set -u` 下直接报 unbound variable。
    die "模拟 ERP 已在运行（pid ${OLD_PID}）。先执行 ./scripts/stop.sh"
  fi
  echo "清理陈旧的 pid 文件（pid $OLD_PID 已不存在）"
  rm -f "$PID_FILE"
fi

mkdir -p "$ROOT_DIR/logs"

# ---------- 后台 ----------
nohup "$PYTHON_BIN" -m uvicorn app.main:app \
  --host "$ERP_HOST" --port "$ERP_PORT" ${RELOAD_ARGS[@]+"${RELOAD_ARGS[@]}"} \
  >>"$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

# 起完立刻探一次，别让「脚本说启动成功、其实端口被占」这种情况蒙混过去。
# 注意健康检查路径是 /health，**不是**主平台那个 /api/v1/health ——
# 模拟 ERP 不套我们的 API 前缀。
for _ in $(seq 1 20); do
  if ! kill -0 "$NEW_PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "启动失败，日志末尾：" >&2
    tail -n 20 "$LOG_FILE" >&2
    exit 1
  fi
  if curl -sf -o /dev/null "http://$ERP_HOST:$ERP_PORT/health" 2>/dev/null; then
    echo "模拟 ERP 已启动  pid=$NEW_PID  http://$ERP_HOST:$ERP_PORT"
    echo "登录页  http://$ERP_HOST:$ERP_PORT/login"
    echo "日志    $LOG_FILE"
    exit 0
  fi
  sleep 0.5
done

echo "进程活着，但 10 秒内没探测到健康检查通过 —— 检查日志：" >&2
tail -n 20 "$LOG_FILE" >&2
exit 1
