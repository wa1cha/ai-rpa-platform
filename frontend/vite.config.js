import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 开发期前端在 5173，后端在 8000。让 dev server 把 /api 转发到后端，
// 浏览器眼里前后端同源 —— 于是后端不用加 CORS 中间件（Phase 6 决定）。
// 生产部署由 nginx 承担同样的转发职责（Phase 7）。
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
