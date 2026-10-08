# ============================================================
# RPA Worker 镜像 —— Phase 7 部署用
#
# Worker 用 Playwright 驱动浏览器去点模拟 ERP。
#
# 基镜像用 **python:3.12-slim-bookworm**，浏览器在构建时自己装：
#   - 为什么不用官方的 mcr.microsoft.com/playwright 镜像：那套镜像在国内
#     （腾讯云实测）层下载只有 ~78 KiB/s，2GB 要拉几个小时，基本不可用。
#     换成 slim 后浏览器从 PLAYWRIGHT_DOWNLOAD_HOST 拉，国内实测 ~12 MB/s。
#   - 为什么必须是 **bookworm**（不是 trixie）：playwright 的 `install --with-deps`
#     依赖清单针对 Debian 12(bookworm) 写；trixie 上 ttf-unifont 等包名已改，
#     apt 会 "no installation candidate" 直接失败。
#
# ⚠️ playwright 的 pip 版本必须与浏览器 revision 配对（v1.49.1 ↔ chromium-1148）。
# 见下面 PLAYWRIGHT_VERSION；版本错配运行时会「找不到浏览器」。
#
# Worker 与主平台之间**只有 HTTP 一条通道**（《需求规格》关键约束）：不连 MySQL、
# 不连 Redis。所以 rpa/requirements.txt 里没有那些依赖，这个镜像里也不会有。
#
# 构建上下文是 rpa/（见 docker-compose.yml 的 `context: ./rpa`）。
# ============================================================

FROM python:3.12-slim-bookworm

# ---- 可选镜像源（构建参数）---- 详见 backend/Dockerfile 同段注释。
# rpa 多一个浏览器下载源 PLAYWRIGHT_DOWNLOAD_HOST：默认 azureedge（全球 CDN，
# 国内实测 ~12 MB/s）；playwright 默认的 cdn.playwright.dev 在国内很慢。
ARG APT_MIRROR=
ARG PIP_INDEX_URL=
ARG PIP_TRUSTED_HOST=
ARG PLAYWRIGHT_DOWNLOAD_HOST=https://playwright.azureedge.net
RUN if [ -n "$APT_MIRROR" ]; then \
      sed -i "s|deb.debian.org|${APT_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true; \
    fi

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PYTHONPATH=/app \
    PLAYWRIGHT_DOWNLOAD_HOST=${PLAYWRIGHT_DOWNLOAD_HOST:-https://playwright.azureedge.net}

WORKDIR /app

COPY requirements.txt ./requirements.txt

# playwright 的 pip 版本必须与浏览器 revision 配对（v1.49.1 ↔ chromium-1148），
# 所以先把它从 requirements 里滤掉，再单独按这个固定版本装。
# 若让 pip 按 requirements 的 `>=1.49,<2.0` 装个更新的版本，它会去找一个
# 没装的浏览器 revision，运行时直接「找不到浏览器」。
ARG PLAYWRIGHT_VERSION=1.49.1
RUN grep -viE '^[[:space:]]*playwright' requirements.txt > /tmp/req.txt \
 && pip install ${PIP_INDEX_URL:+--index-url ${PIP_INDEX_URL}} \
                ${PIP_TRUSTED_HOST:+--trusted-host ${PIP_TRUSTED_HOST}} \
                --no-cache-dir -r /tmp/req.txt \
 && pip install ${PIP_INDEX_URL:+--index-url ${PIP_INDEX_URL}} \
                ${PIP_TRUSTED_HOST:+--trusted-host ${PIP_TRUSTED_HOST}} \
                --no-cache-dir "playwright==${PLAYWRIGHT_VERSION}" \
 && rm /tmp/req.txt

# 装 Chromium 本体 + 全部系统依赖（--with-deps 会走 apt 装 OS 依赖）。
# 注意：1.49 会同时拉 chromium 与 chromium-headless-shell（无头模式用的是后者）。
RUN playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

# rpa 是顶层包（内部用 `from rpa.common import ...`），所以放到 /app/rpa 并让
# PYTHONPATH=/app，`python -m rpa.main` 才找得到。
COPY . /app/rpa/

# 容器里没有显示器，只能无头跑。compose 会注入 RPA_HEADLESS=true。
CMD ["python", "-m", "rpa.main"]
