# ============================================================
# 前端镜像 —— Phase 7 部署用
#
# 两段构建：
#   ① node 里 npm run build 出静态产物 dist/
#   ② 把 dist/ 交给 nginx 托管，并由 nginx 反代 /api → backend:8000
#
# 为什么由 nginx 反代而不是让前端直连后端：开发期是 Vite dev server 代劳
# （见 vite.config.js 的 server.proxy），后端因此**不加 CORS 中间件**；
# 生产环境把同一职责交给 nginx，前后端在浏览器眼里依旧同源。
#
# 构建上下文是 frontend/（见 docker-compose.yml 的 `context: ./frontend`）。
# ============================================================

# ---------- ① 构建 ----------
FROM node:22-alpine AS build
WORKDIR /app

# 先拷 lockfile 再 npm ci：依赖不变时这层缓存有效，改源码不必重装。
# npm ci 严格按 package-lock.json 装，构建可复现。
COPY package.json package-lock.json ./
RUN npm ci

COPY . ./
RUN npm run build

# ---------- ② 托管 ----------
FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80
