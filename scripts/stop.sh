#!/usr/bin/env bash
#
# 停止后台进程：API 服务（start.sh）+ 作业进程（start_workers.sh）
#             + 模拟 ERP（start_mock_erp.sh）+ RPA Worker（run_rpa_worker.sh）
#
# 用法：
#   ./scripts/stop.sh
#
# 先 SIGTERM 给它们机会优雅退出（关连接池、跑 lifespan 的清理、关浏览器），
# 等 10 秒还不走才 SIGKILL —— 直接 KILL 会让连接池里的 MySQL 连接
# 变成服务端侧的半开连接，要等超时才能回收。
#
# 四个一起停是刻意的：用户喊「停」的时候，想要的是「这套东西别跑了」，
# 而不是记得起来还有第二、第三、第四个 pid 文件。
#
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

stop_one() {
  local name="$1" pid_file="$2"

  if [[ ! -f "$pid_file" ]]; then
    echo "[$name] 没有 $pid_file —— 大概没在后台跑"
    echo "[$name] （如果是 --foreground 起的，直接 Ctrl-C）"
    return 0
  fi

  local pid
  pid="$(cat "$pid_file")"

  if ! kill -0 "$pid" 2>/dev/null; then
    echo "[$name] pid $pid 已不存在，清掉陈旧的 pid 文件"
    rm -f "$pid_file"
    return 0
  fi

  echo "[$name] 正在停止 pid=$pid ..."
  kill -TERM "$pid"

  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done

  if kill -0 "$pid" 2>/dev/null; then
    echo "[$name] 10 秒内没退出，强制 kill -9"
    kill -KILL "$pid"
    sleep 0.5
  fi

  rm -f "$pid_file"
  echo "[$name] 已停止"
}

stop_one "backend" "$ROOT_DIR/logs/backend.pid"
stop_one "workers" "$ROOT_DIR/logs/workers.pid"
stop_one "mock-erp" "$ROOT_DIR/logs/mock_erp.pid"
stop_one "rpa-worker" "$ROOT_DIR/logs/rpa_worker.pid"
