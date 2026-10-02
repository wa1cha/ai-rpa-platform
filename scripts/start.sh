#!/usr/bin/env bash
#
# 后台启动后端 API 服务
#
# 用法：
#   ./scripts/start.sh              后台启动（日志写 logs/backend.log）
#   ./scripts/start.sh --foreground 前台启动，Ctrl-C 停（调试用）
#
# 说明：
#   - 解释器从 .env 的 PYTHON_BIN 读，脚本里不写死任何一台机器的路径
#   - 监听地址从应用配置读，不在这里重复一遍默认值
#   - 已在运行时拒绝重复启动，避免两个进程抢同一个端口
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
PID_FILE="$ROOT_DIR/logs/backend.pid"
LOG_FILE="$ROOT_DIR/logs/backend.log"

die() { echo "错误：$*" >&2; exit 1; }

# ---------- 解释器 ----------
[[ -f "$ENV_FILE" ]] || die "找不到 $ENV_FILE，请先执行：cp .env.example .env"

# 只取需要的键，避免把 .env 当脚本执行
value_of() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }

PYTHON_BIN="$(value_of PYTHON_BIN)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
[[ -x "$PYTHON_BIN" ]] || die "解释器不存在或不可执行：$PYTHON_BIN
  在 .env 里把 PYTHON_BIN 改成装了依赖的那个绝对路径。"

cd "$ROOT_DIR/backend"

# ---------- 监听地址：问应用自己要 ----------
# 不在这里写默认值，否则 .env 改了 APP_PORT、脚本还按 8000 起，
# 两边会各说各话。读失败（比如 .env 里有语法错）就直接退出，早失败早发现。
IFS=' ' read -r APP_HOST APP_PORT DEBUG < <(
  PYTHONPATH=. "$PYTHON_BIN" -c \
    'from app.core.config import settings; print(settings.app_host, settings.app_port, settings.debug)'
) || die "读应用配置失败，先单独跑一次上面的 python 看报什么错"

RELOAD_ARGS=()
[[ "$DEBUG" == "True" ]] && RELOAD_ARGS=(--reload)

# ---------- 前台 ----------
if [[ "${1:-}" == "--foreground" ]]; then
  echo "前台启动 http://$APP_HOST:$APP_PORT （Ctrl-C 停止）"
  exec "$PYTHON_BIN" -m uvicorn app.main:app \
    --host "$APP_HOST" --port "$APP_PORT" "${RELOAD_ARGS[@]}"
fi

# ---------- 已经在跑？ ----------
if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE")"
  if kill -0 "$OLD_PID" 2>/dev/null; then
    # ${OLD_PID} 必须带花括号：紧跟其后的全角「）」在 bash 眼里算标识符字符，
    # 不括起来会被吃进变量名，`set -u` 下直接报 unbound variable。
    die "服务已在运行（pid ${OLD_PID}）。先执行 ./scripts/stop.sh"
  fi
  # pid 文件在、进程不在 —— 上次异常退出留下的残骸，清掉继续
  echo "清理陈旧的 pid 文件（pid $OLD_PID 已不存在）"
  rm -f "$PID_FILE"
fi

mkdir -p "$ROOT_DIR/logs"

# ---------- 后台 ----------
nohup "$PYTHON_BIN" -m uvicorn app.main:app \
  --host "$APP_HOST" --port "$APP_PORT" "${RELOAD_ARGS[@]}" \
  >>"$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

# 起完立刻探一次，别让「脚本说启动成功、其实端口被占」这种情况蒙混过去
for _ in $(seq 1 20); do
  if ! kill -0 "$NEW_PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "启动失败，日志末尾：" >&2
    tail -n 20 "$LOG_FILE" >&2
    exit 1
  fi
  if curl -sf -o /dev/null "http://$APP_HOST:$APP_PORT/api/v1/health" 2>/dev/null; then
    echo "已启动  pid=$NEW_PID  http://$APP_HOST:$APP_PORT"
    echo "接口文档 http://$APP_HOST:$APP_PORT/docs"
    echo "日志     $LOG_FILE"
    exit 0
  fi
  sleep 0.5
done

echo "进程活着，但 10 秒内没探测到健康检查通过 —— 检查日志：" >&2
tail -n 20 "$LOG_FILE" >&2
exit 1
