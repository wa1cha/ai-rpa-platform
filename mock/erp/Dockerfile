# ============================================================
# 模拟 ERP 镜像 —— Phase 7 部署用
#
# 模拟 ERP 扮演「别人家的老系统」：独立 FastAPI 应用、独立库 mock_erp、
# 服务端渲染页面。依赖刻意比主平台精简（无 redis / 无 PyJWT / 无 openpyxl）。
#
# ⚠️ 目录层级必须保住仓库的样子（/app/mock/erp/...），不能拍平成 /app/...：
# app/config.py 用 `Path(__file__).resolve().parents[3]` 推项目根，main.py 又用
# `PROJECT_ROOT / "mock" / "erp" / "static"` 定位静态目录。拍平后 parents[3] 越界，
# 应用一启动就 IndexError。
#
# 构建上下文是 mock/erp/（见 docker-compose.yml 的 `context: ./mock/erp`）。
# ============================================================

FROM python:3.12-slim

# ---- 可选镜像源（构建参数）---- 详见 backend/Dockerfile 同段注释。
ARG APT_MIRROR=
ARG PIP_INDEX_URL=
ARG PIP_TRUSTED_HOST=
RUN if [ -n "$APT_MIRROR" ]; then \
      sed -i "s|deb.debian.org|${APT_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true; \
    fi

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app/mock/erp

COPY requirements.txt ./requirements.txt
# 与 app 镜像同理：asyncmy / bcrypt 在 PyPI 上有 manylinux wheel；若报缺编译器，
# 在 pip install 之前补 `apt-get install -y --no-install-recommends gcc python3-dev`。
RUN pip install ${PIP_INDEX_URL:+--index-url ${PIP_INDEX_URL}} \
                ${PIP_TRUSTED_HOST:+--trusted-host ${PIP_TRUSTED_HOST}} \
                --no-cache-dir -r requirements.txt

COPY . ./

RUN useradd --create-home --shell /usr/sbin/nologin appuser \
 && chown -R appuser:appuser /app
USER appuser

EXPOSE 8001

# 监听 0.0.0.0：默认配置里 mock_erp_host=127.0.0.1，容器里等于「只监听自己」，
# 同网络的 rpa-worker 连不上。由 compose 注入 MOCK_ERP_HOST=0.0.0.0。
CMD ["sh", "-c", "exec uvicorn app.main:app --host ${MOCK_ERP_HOST:-0.0.0.0} --port ${MOCK_ERP_PORT:-8001}"]
