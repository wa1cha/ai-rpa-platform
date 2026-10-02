# ============================================================
# 后端 API 镜像 —— Phase 7 部署用
#
# ⚠️ 状态：**未经运行验证**。
#    写这个文件时本机没有装 Docker，无法 `docker build` 跑一遍，
#    所以它是「按仓库现状静态推出来」的。第一次真正构建时请重点核对：
#      ① 依赖能否装上 —— asyncmy / bcrypt 有没有 cp312 的 manylinux wheel。
#         若报「找不到编译器」，就在 pip install 之前补一句
#         `apt-get install -y --no-install-recommends gcc`（见文末说明）。
#      ② 时区是否生效 —— 见下面的 TZ 段。
#      ③ 健康检查能否通过 —— 它依赖 APP_HOST=0.0.0.0，由 compose 注入。
#
# 构建上下文是**仓库根目录**（见 docker-compose.yml 里的 `context: .`），
# 因此下面的 COPY 路径都从仓库根起算，不是从 backend/ 起算。
# ============================================================

FROM python:3.12-slim

# PYTHONUNBUFFERED 是必须的：不设的话 stdout 走块缓冲，
# `docker compose logs -f` 会看起来像「服务卡住不输出」，其实只是没刷出来。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 时区必须和 MySQL 容器一致（compose 给 mysql 传了 TZ=Asia/Shanghai）。
#
# 本项目的时间字段来自两个地方：Python 的 `datetime.now()`（queued_at、
# finished_at、reviewed_at）和 MySQL 的 `NOW()`（created_at、updated_at）。
# 容器默认是 UTC 而 MySQL 容器是 +08:00 —— 不统一的话，同一条任务里
# created_at 和 queued_at 会差 8 小时，排查时会先怀疑是不是并发写坏了。
ENV TZ=Asia/Shanghai
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先只拷依赖清单再装：源码改动不会让这一层缓存失效，重新构建时不必重装依赖。
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 再拷源码。`COPY backend/ ./` 让 app 包落在 /app/app，
# 与下面 `uvicorn app.main:app` 的模块路径对得上。
COPY backend/ ./

# 不用 root 跑服务。被攻破时能拿到的权限越小越好，
# 而且容器里 root 的 capabilities 比宿主机 root 还多。
RUN useradd --create-home --shell /usr/sbin/nologin appuser \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 用 shell 形式（而不是 exec 形式的 JSON 数组）是为了让 ${APP_HOST} 被展开 ——
# exec 形式不经过 shell，变量不会被替换。
# 里面的 `exec` 让 uvicorn **接替 sh 成为 PID 1**：否则 sh 才是 PID 1，
# `docker stop` 的 SIGTERM 发给 sh、传不到 uvicorn，要等 10 秒超时被 SIGKILL ——
# 那样 main.py lifespan 里的「释放 MySQL / Redis 连接池」根本不会执行。
#
# 默认值 0.0.0.0 只是兜底：应用配置里 app_host 的默认值是 127.0.0.1，
# 在容器里等于「只监听自己」，宿主机和健康检查都连不上。
# 真正的来源是 compose 显式注入的 APP_HOST: 0.0.0.0。
CMD ["sh", "-c", "exec uvicorn app.main:app --host ${APP_HOST:-0.0.0.0} --port ${APP_PORT:-8000}"]

# ------------------------------------------------------------
# 如果 ① 真的发生了（pip 装 asyncmy / bcrypt 时报缺编译器）：
# 把上面那条 RUN pip install 拆成两段，装完就把编译工具卸掉，别留在最终镜像里：
#
#   RUN apt-get update \
#    && apt-get install -y --no-install-recommends gcc python3-dev \
#    && pip install --no-cache-dir -r requirements.txt \
#    && apt-get purge -y --auto-remove gcc python3-dev \
#    && rm -rf /var/lib/apt/lists/*
#
# 先不加是为了让镜像小、构建快。这两个包在 PyPI 上都有 manylinux wheel，
# 正常情况下根本用不到编译器 —— 所以是「出问题了再加」，不是默认带上。
# ------------------------------------------------------------
