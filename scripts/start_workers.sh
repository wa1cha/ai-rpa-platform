#!/usr/bin/env bash
#
# 后台启动作业进程（僵尸回收等定时任务），与 scripts/start.sh 是同一套骨架
#
# 用法：
#   ./scripts/start_workers.sh              后台启动（日志写 logs/workers.log）
#   ./scripts/start_workers.sh --foreground 前台启动，Ctrl-C 停（调试用）
#
# 与 start.sh 的两点不同：
#   - 没有端口要探，所以「启动成功」的判据换成「进程还在 + 日志里出现了启动行」
#   - 作业进程**只需要一份**（见 workers/main.py 的说明），所以这里同样拒绝重复启动
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
PID_FILE="$ROOT_DIR/logs/workers.pid"
LOG_FILE="$ROOT_DIR/logs/workers.log"

die() { echo "错误：$*" >&2; exit 1; }

[[ -f "$ENV_FILE" ]] || die "找不到 $ENV_FILE，请先执行：cp .env.example .env"

# 只取需要的键，避免把 .env 当脚本执行
value_of() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }

PYTHON_BIN="$(value_of PYTHON_BIN)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
[[ -x "$PYTHON_BIN" ]] || die "解释器不存在或不可执行：$PYTHON_BIN
  在 .env 里把 PYTHON_BIN 改成装了依赖的那个绝对路径。"

cd "$ROOT_DIR/backend"

# ---------- 前台 ----------
if [[ "${1:-}" == "--foreground" ]]; then
  echo "前台启动作业进程（Ctrl-C 停止）"
  exec env PYTHONPATH=. "$PYTHON_BIN" -m app.workers.main
fi

# ---------- 已经在跑？ ----------
if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE")"
  if kill -0 "$OLD_PID" 2>/dev/null; then
    die "作业进程已在运行（pid ${OLD_PID}）。先执行 ./scripts/stop.sh"
  fi
  echo "清理陈旧的 pid 文件（pid $OLD_PID 已不存在）"
  rm -f "$PID_FILE"
fi

mkdir -p "$ROOT_DIR/logs"

# 记录启动前的日志行数 —— 探针只看**新增**的行。
# 日志是追加的，若 grep 整个文件，上一次运行留下的启动行会让这里立刻误报
# 「已启动」，即使新进程秒退也蒙混过关。
LOG_LINES_BEFORE="$(wc -l < "$LOG_FILE" 2>/dev/null || echo 0)"

# ---------- 后台 ----------
nohup env PYTHONPATH=. "$PYTHON_BIN" -m app.workers.main \
  >>"$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

# ---------- 探一次 ----------
# 作业进程不监听端口，没法像 start.sh 那样 curl 健康检查。
# 判据分两步：进程必须还活着，且日志里出现了启动行 —— 只有前者的话，
# 「import 就崩」的进程也能骗过这一关（它要等到真的开始跑作业才报错）。
for _ in $(seq 1 20); do
  if ! kill -0 "$NEW_PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "启动失败，日志末尾：" >&2
    tail -n 20 "$LOG_FILE" >&2
    exit 1
  fi
  if tail -n +"$((LOG_LINES_BEFORE + 1))" "$LOG_FILE" 2>/dev/null | grep -q "后台作业进程启动"; then
    echo "已启动  pid=$NEW_PID"
    echo "日志     $LOG_FILE"
    exit 0
  fi
  sleep 0.5
done

echo "进程活着，但 10 秒内没看到启动日志 —— 检查日志：" >&2
tail -n 20 "$LOG_FILE" >&2
exit 1
