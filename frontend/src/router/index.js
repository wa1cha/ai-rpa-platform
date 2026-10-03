import { createRouter, createWebHistory } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

// 除 /login 外全部挂在 MainLayout 下（侧栏 + 顶栏 + 内容区）。
// 页面组件懒加载：只有真正访问某个路由时才会去解析对应的 .vue。
const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/pages/Login.vue'),
    meta: { public: true, title: '登录' },
  },
  {
    path: '/',
    component: () => import('@/layouts/MainLayout.vue'),
    children: [
      { path: '', redirect: { name: 'dashboard' } },
      {
        path: 'dashboard',
        name: 'dashboard',
        component: () => import('@/pages/Dashboard.vue'),
        meta: { title: '看板' },
      },
      {
        path: 'orders',
        name: 'orders',
        component: () => import('@/pages/Orders.vue'),
        meta: { title: '订单' },
      },
      {
        path: 'orders/:id',
        name: 'order-detail',
        component: () => import('@/pages/OrderDetail.vue'),
        meta: { title: '订单详情' },
      },
      {
        path: 'tasks',
        name: 'tasks',
        component: () => import('@/pages/Tasks.vue'),
        meta: { title: '任务' },
      },
      {
        path: 'tasks/:id',
        name: 'task-detail',
        component: () => import('@/pages/TaskDetail.vue'),
        meta: { title: '任务详情' },
      },
      {
        path: 'ai-analyses',
        name: 'ai-analyses',
        component: () => import('@/pages/AiAnalyses.vue'),
        meta: { title: 'AI 分析' },
      },
      {
        path: 'import-batches',
        name: 'import-batches',
        component: () => import('@/pages/ImportBatches.vue'),
        meta: { title: '导入批次' },
      },
      {
        path: 'notifications',
        name: 'notifications',
        component: () => import('@/pages/Notifications.vue'),
        meta: { title: '通知' },
      },
    ],
  },
  { path: '/:pathMatch(.*)*', redirect: { name: 'dashboard' } },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (!to.meta.public && !auth.isAuthenticated) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  // 已登录还想去登录页，直接送回首页。
  if (to.name === 'login' && auth.isAuthenticated) {
    return { name: 'dashboard' }
  }
  return true
})

router.afterEach((to) => {
  const base = 'AI-RPA 订单自动化平台'
  document.title = to.meta.title ? `${to.meta.title} · ${base}` : base
})

export default router
