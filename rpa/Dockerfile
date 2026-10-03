# ============================================================
# RPA Worker 镜像 —— Phase 7 部署用
#
# Worker 用 Playwright 驱动浏览器去点模拟 ERP，所以基镜像用 **Playwright 官方镜像**：
# 它已内置 Chromium 与全部系统依赖（装在 /ms-playwright），省去自己装浏览器容易踩的坑。
#
# ⚠️ 它**只内置浏览器、不内置 playwright 这个 pip 包**：装依赖时必须自己把
# `playwright` 装上，且版本要和镜像标签（v1.49.1 → chromium-1148）严格对齐，
# 否则运行时会「找不到浏览器」。见下面 PLAYWRIGHT_VERSION。
#
# Worker 与主平台之间**只有 HTTP 一条通道**（《需求规格》关键约束）：不连 MySQL、
# 不连 Redis。所以 rpa/requirements.txt 里没有那些依赖，这个镜像里也不会有。
#
# 构建上下文是 rpa/（见 docker-compose.yml 的 `context: ./rpa`）。
# ============================================================

FROM mcr.microsoft.com/playwright/python:v1.49.1-jammy

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PYTHONPATH=/app

WORKDIR /app

COPY requirements.txt ./requirements.txt

# playwright 的 pip 版本必须与基镜像标签配对（v1.49.1 ↔ chromium-1148），
# 所以先把它从 requirements 里滤掉，再单独按这个固定版本装。
# 若让 pip 按 requirements 的 `>=1.49,<2.0` 装个更新的版本，它会去找一个
# 镜像里不存在的浏览器 revision，运行时直接「找不到浏览器」。
ARG PLAYWRIGHT_VERSION=1.49.1
RUN grep -viE '^[[:space:]]*playwright' requirements.txt > /tmp/req.txt \
 && pip install --no-cache-dir -r /tmp/req.txt \
 && pip install --no-cache-dir "playwright==${PLAYWRIGHT_VERSION}" \
 && rm /tmp/req.txt

# rpa 是顶层包（内部用 `from rpa.common import ...`），所以放到 /app/rpa 并让
# PYTHONPATH=/app，`python -m rpa.main` 才找得到。
COPY . /app/rpa/

# 容器里没有显示器，只能无头跑。compose 会注入 RPA_HEADLESS=true。
CMD ["python", "-m", "rpa.main"]
