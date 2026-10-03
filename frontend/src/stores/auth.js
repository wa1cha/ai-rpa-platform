import { defineStore } from 'pinia'

import * as authApi from '@/api/auth'
import { clearAuth, getToken, getUser, setAuth } from '@/api/request'

// token 与 user 在 localStorage 持久化，刷新页面后从 getToken()/getUser() 恢复，
// 所以 state 初值直接读本地 —— 不需要额外的「初始化」步骤。
export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: getToken(),
    user: getUser(),
  }),

  getters: {
    isAuthenticated: (state) => Boolean(state.token),
    username: (state) => state.user?.username || '',
    role: (state) => state.user?.role || '',
  },

  actions: {
    async login(username, password) {
      const data = await authApi.login(username, password)
      this.token = data.access_token
      this.user = data.user
      setAuth(data.access_token, data.user)
      return data
    },

    // 用服务端最新数据刷新本地 user（比如角色被改过）。
    async fetchMe() {
      const detail = await authApi.me()
      this.user = detail
      setAuth(this.token, detail)
      return detail
    },

    async logout() {
      // JWT 无状态，服务端登出只是写审计日志。即便这一枪没打中，
      // 本地登录态也必须清掉 —— 不能让网络失败挡住「退出」。
      try {
        await authApi.logout()
      } catch {
        /* 忽略：本地登出不应被网络失败阻断 */
      }
      this.token = null
      this.user = null
      clearAuth()
    },
  },
})
