import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import router from './router'
import './styles/index.css'

// pinia 先于 router 安装：路由守卫里会 useAuthStore()，
// 本地开发热重载时若顺序反了会拿到「no active pinia」。
const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
