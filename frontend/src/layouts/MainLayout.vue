<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import { toastSuccess } from '@/utils/toast'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const navItems = [
  { name: 'dashboard', label: '看板', icon: '▤' },
  { name: 'orders', label: '订单', icon: '☰' },
  { name: 'tasks', label: '任务', icon: '⚙' },
  { name: 'ai-analyses', label: 'AI 分析', icon: '✦' },
  { name: 'import-batches', label: '导入批次', icon: '⇪' },
  { name: 'notifications', label: '通知', icon: '✉' },
]

// 详情页（orders/:id、tasks/:id）也应高亮所属的一级菜单。
const activeName = computed(() => {
  const matched = route.matched.find((r) => r.name)
  const name = matched?.name
  if (name === 'order-detail') return 'orders'
  if (name === 'task-detail') return 'tasks'
  return name
})

const pageTitle = computed(() => route.meta.title || '')

async function onLogout() {
  await auth.logout()
  toastSuccess('已退出登录')
  router.replace({ name: 'login' })
}
</script>

<template>
  <div class="layout">
    <aside class="sidebar">
      <div class="sidebar__brand">AI-RPA 平台</div>
      <nav class="sidebar__nav">
        <router-link
          v-for="item in navItems"
          :key="item.name"
          class="nav-item"
          :class="{ 'nav-item--active': activeName === item.name }"
          :to="{ name: item.name }"
        >
          <span class="nav-item__icon">{{ item.icon }}</span>
          <span>{{ item.label }}</span>
        </router-link>
      </nav>
    </aside>

    <div class="main">
      <header class="topbar">
        <div class="topbar__title">{{ pageTitle }}</div>
        <div class="topbar__right">
          <span>{{ auth.username }}</span>
          <span class="tag tag--blue">{{ auth.role }}</span>
          <button class="btn btn--sm" @click="onLogout">退出</button>
        </div>
      </header>

      <main class="content">
        <router-view />
      </main>
    </div>
  </div>
</template>
