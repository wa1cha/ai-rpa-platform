# ============================================================
# 应用镜像 —— backend / workers / seed 三个服务共用
#
# 三者跑的是同一份代码、同一套依赖（backend 主平台 + 被它 import 的 ai 包），
# 差别只在启动命令。所以 compose 里用同一 image、不同 command：
#   backend → uvicorn app.main:app
#   workers → python -m app.workers.main
#   seed    → 串跑 scripts/seed_*.py 后退出
#
# 关键点：**ai 包必须装进镜像**。backend 代码 `import ai`，而 ai/ 是独立发行包
# （见 ai/pyproject.toml），本机开发靠 `pip install -e ai/` 装进解释器。
# 镜像里用**普通安装**（COPY + pip install ./ai）把它固化成镜像的一部分 ——
# 运行时不挂宿主机源码，也不靠 sys.path 兜底。
#
# 构建上下文是**仓库根目录**（见 docker-compose.yml 的 `context: .`），
# 所以下面的 COPY 路径都从仓库根起算。根 .dockerignore 已放开 ai/、scripts/、
# database/seed/，并排掉 frontend/、rpa/、mock/ 等无关目录。
# ============================================================

FROM python:3.12-slim

# PYTHONUNBUFFERED 是必须的：不设的话 stdout 走块缓冲，
# `docker compose logs -f` 会看起来像「服务卡住不输出」，其实只是没刷出来。
# PYTHONPATH 指向 backend/，让 `app.*` 能被 import（与各启动脚本的 PYTHONPATH 口径一致）。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    PYTHONPATH=/app/backend

# 时区必须和 MySQL 容器一致（compose 给 mysql 传了 TZ=Asia/Shanghai）。
# 本项目时间来自两处：Python 的 datetime.now() 和 MySQL 的 NOW()；容器默认 UTC
# 而 MySQL 是 +08:00，不统一的话同一条任务里 created_at 与 queued_at 会差 8 小时。
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ① 先装 ai 包。它与 backend 是单向依赖（backend → ai），backend 一跑起来就 import 它。
COPY ai/ ./ai/
RUN pip install --no-cache-dir ./ai

# ② 再装主平台依赖。放在源码之前，改代码不会让依赖层缓存失效。
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r ./backend/requirements.txt

# ③ 源码与种子脚本。脚本里的 `BACKEND_DIR = scripts/../backend` 在本布局下成立。
#    database/seed/04_orders_sample.csv 给 seed 服务导入 7 笔样例订单用。
COPY backend/ ./backend/
COPY scripts/ ./scripts/
COPY database/seed/04_orders_sample.csv ./database/seed/04_orders_sample.csv

# 截图目录：RPA 上传的失败截图由 backend 存在这里（见 core/config.py 的 screenshot_dir）。
# 先建好并 chown 给 appuser —— compose 那个命名卷首次挂载到这里时会继承属主，
# 否则卷属主是 root，appuser 写不进去。
RUN mkdir -p /app/backend/var/screenshots \
 && useradd --create-home --shell /usr/sbin/nologin appuser \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 用 shell 形式（而不是 exec 的 JSON 数组）是为了让 ${APP_HOST} 被展开。
# 里面的 `exec` 让 uvicorn 接替 sh 成为 PID 1：否则 docker stop 的 SIGTERM
# 发给 sh、传不到 uvicorn，要等 10 秒超时被 SIGKILL，main.py lifespan 里的
# 「释放连接池」就不会执行。APP_HOST 由 compose 注入 0.0.0.0（默认值只是兜底）。
CMD ["sh", "-c", "exec uvicorn app.main:app --host ${APP_HOST:-0.0.0.0} --port ${APP_PORT:-8000}"]

# ------------------------------------------------------------
# 若 pip 装 asyncmy / bcrypt 时报缺编译器，把 ② 拆成：
#   RUN apt-get update \
#    && apt-get install -y --no-install-recommends gcc python3-dev \
#    && pip install --no-cache-dir -r ./backend/requirements.txt \
#    && apt-get purge -y --auto-remove gcc python3-dev \
#    && rm -rf /var/lib/apt/lists/*
# 这两个包在 PyPI 上都有 manylinux wheel，正常用不到编译器 —— 出问题再加。
# ------------------------------------------------------------
