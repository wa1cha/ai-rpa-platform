#!/usr/bin/env bash
#
# Phase 7 Docker 部署包装 —— 常见动作的快捷入口
#
# 用法：
#   ./scripts/deploy.sh up      # 构建并起全部服务（默认动作）
#   ./scripts/deploy.sh build   # 只构建镜像
#   ./scripts/deploy.sh down    # 停全部（保留数据卷）
#   ./scripts/deploy.sh reset   # 停并清空数据卷（下次会重新初始化 + 灌种子）
#   ./scripts/deploy.sh logs    # 跟踪日志
#   ./scripts/deploy.sh ps      # 看各服务状态
#
# 起好后访问 http://localhost:8080 （只有前端对外）。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "错误：找不到 docker 命令。Phase 7 需要 Docker Desktop。" >&2
  exit 1
fi

action="${1:-up}"
case "$action" in
  build) docker compose build ;;
  up)    docker compose up -d --build ;;
  down)  docker compose down ;;
  reset) docker compose down -v ;;
  logs)  docker compose logs -f ;;
  ps)    docker compose ps ;;
  *)
    echo "用法：$0 {up|build|down|reset|logs|ps}" >&2
    exit 1
    ;;
esac
