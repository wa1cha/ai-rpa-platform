<script setup>
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { useAuthStore } from '@/stores/auth'
import { toastSuccess } from '@/utils/toast'

const router = useRouter()
const route = useRoute()
const auth = useAuthStore()

const username = ref('')
const password = ref('')
const loading = ref(false)
const errorMessage = ref('')

async function onSubmit() {
  if (!username.value || !password.value) {
    errorMessage.value = '请输入用户名和密码'
    return
  }
  loading.value = true
  errorMessage.value = ''
  try {
    await auth.login(username.value, password.value)
    toastSuccess('登录成功')
    // 登录前被守卫拦下的目标地址保存在 query.redirect 里，登录后送回去。
    const redirect = route.query.redirect
    router.replace(typeof redirect === 'string' ? redirect : { name: 'dashboard' })
  } catch (err) {
    // 401 + code 4002 是「用户名或密码错误」，拦截器已经弹过一次 toast，
    // 这里再把文案落到表单里，两条通道不冲突。
    errorMessage.value = err?.message || '登录失败，请稍后重试'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <form class="login-card" @submit.prevent="onSubmit">
      <h1 class="login-card__title">AI-RPA 订单自动化平台</h1>
      <p class="login-card__subtitle">管理后台 · 请登录</p>

      <div class="field">
        <label class="field__label" for="username">用户名</label>
        <input
          id="username"
          v-model.trim="username"
          class="input"
          type="text"
          autocomplete="username"
          placeholder="请输入用户名"
        />
      </div>

      <div class="field">
        <label class="field__label" for="password">密码</label>
        <input
          id="password"
          v-model="password"
          class="input"
          type="password"
          autocomplete="current-password"
          placeholder="请输入密码"
        />
      </div>

      <p v-if="errorMessage" class="login-card__error" style="color: var(--c-red); margin-bottom: 12px">
        {{ errorMessage }}
      </p>

      <button class="btn btn--primary" style="width: 100%" type="submit" :disabled="loading">
        {{ loading ? '登录中…' : '登录' }}
      </button>
    </form>
  </div>
</template>
