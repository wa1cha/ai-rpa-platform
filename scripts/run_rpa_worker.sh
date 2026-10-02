#!/usr/bin/env bash
#
# 启停 RPA Worker，与 scripts/start_workers.sh 是同一套骨架
#
# 用法：
#   ./scripts/run_rpa_worker.sh                          后台跑（日志写 logs/rpa_worker.log）
#   ./scripts/run_rpa_worker.sh --foreground --headed    前台跑 + 打开浏览器，演示用
#   ./scripts/run_rpa_worker.sh --foreground --once      跑一笔就退
#
# 其余参数全部原样透传给 `python -m rpa.main`（--once / --headed /
# --worker-name / --log-level），这个脚本只管进程管理，不重复实现参数解析。
#
# 与 start_workers.sh 的两点不同：
#   - **cwd 是仓库根目录**，不是 backend/：`rpa` 是根目录下的包，需要
#     `PYTHONPATH=.` 从根目录 import。它和 backend/app 没有任何 import 关系
#     （Worker 只跟主平台说 HTTP，不碰数据库 —— 见 rpa/requirements.txt）。
#   - 启动探针 grep 的是 `rpa/common/logger.py` 里的 STARTED_MARKER。
#     这一行打**在登录主平台、开起浏览器之后**，所以探针命中意味着
#     「真的能干活了」，而不只是「进程还在」。进程活着但连不上主平台的
#     情况（它会打日志然后退出）骗不过这一关。
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
PID_FILE="$ROOT_DIR/logs/rpa_worker.pid"
LOG_FILE="$ROOT_DIR/logs/rpa_worker.log"

#: 必须与 rpa/common/logger.py 的 STARTED_MARKER 一致。
STARTED_MARKER="RPA Worker 启动完成，开始领任务"

die() { echo "错误：$*" >&2; exit 1; }

# ---------- 切参数：--foreground 归本脚本，其余透传给 Worker ----------
FOREGROUND=0
WORKER_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --foreground) FOREGROUND=1 ;;
    *) WORKER_ARGS+=("$arg") ;;
  esac
done

[[ -f "$ENV_FILE" ]] || die "找不到 $ENV_FILE，请先执行：cp .env.example .env"

# 只取需要的键，避免把 .env 当脚本执行
value_of() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2-; }

PYTHON_BIN="$(value_of PYTHON_BIN)"
PYTHON_BIN="${PYTHON_BIN:-python3}"
[[ -x "$PYTHON_BIN" ]] || die "解释器不存在或不可执行：$PYTHON_BIN
  在 .env 里把 PYTHON_BIN 改成装了依赖的那个绝对路径。"

cd "$ROOT_DIR"

# ${ARR[@]+"${ARR[@]}"} 是给 bash 3.2（macOS 自带那个）的写法：`set -u` 下
# 展开一个空数组会被当成 unbound variable —— 而「加不加引号」的直觉在这里
# 不成立。没有参数时它展开成空，正好。
if [[ "$FOREGROUND" -eq 1 ]]; then
  echo "前台启动 RPA Worker（Ctrl-C 停止）"
  exec env PYTHONPATH="$ROOT_DIR" "$PYTHON_BIN" -m rpa.main ${WORKER_ARGS[@]+"${WORKER_ARGS[@]}"}
fi

# ---------- 已经在跑？ ----------
if [[ -f "$PID_FILE" ]]; then
  OLD_PID="$(cat "$PID_FILE")"
  if kill -0 "$OLD_PID" 2>/dev/null; then
    die "RPA Worker 已在运行（pid ${OLD_PID}）。先执行 ./scripts/stop.sh"
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
nohup env PYTHONPATH="$ROOT_DIR" "$PYTHON_BIN" -m rpa.main \
  ${WORKER_ARGS[@]+"${WORKER_ARGS[@]}"} \
  >>"$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

# ---------- 探一次 ----------
# 第一次启动要下载/落盘 Chromium 的上下文，比作业进程慢，所以给 20 秒。
for _ in $(seq 1 40); do
  if ! kill -0 "$NEW_PID" 2>/dev/null; then
    rm -f "$PID_FILE"
    echo "启动失败，日志末尾：" >&2
    tail -n 20 "$LOG_FILE" >&2
    exit 1
  fi
  if tail -n +"$((LOG_LINES_BEFORE + 1))" "$LOG_FILE" 2>/dev/null | grep -q "$STARTED_MARKER"; then
    echo "已启动  pid=$NEW_PID"
    echo "日志     $LOG_FILE"
    exit 0
  fi
  sleep 0.5
done

echo "进程活着，但 20 秒内没看到启动日志 —— 检查日志：" >&2
tail -n 20 "$LOG_FILE" >&2
exit 1
