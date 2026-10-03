// axios 实例 + 统一响应拦截。后端所有接口都是 `{code,message,data}` 外壳
// （见 schemas/common.py），所以取值只在这一层解一次，上层拿到的就是 data 本身。
import axios from 'axios'

import { toastError } from '@/utils/toast'

export const TOKEN_KEY = 'ai_rpa_token'
export const USER_KEY = 'ai_rpa_user'

export class ApiError extends Error {
  constructor(code, message) {
    super(message)
    this.name = 'ApiError'
    this.code = code
  }
}

// ---------- 本地凭证存取：request.js 与 stores/auth.js 共用 ----------
// 放在这里而不是 store 里，是为了避免「store → api → store」的循环 import。

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function getUser() {
  const raw = localStorage.getItem(USER_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw)
  } catch {
    return null
  }
}

export function setAuth(token, user) {
  if (token) localStorage.setItem(TOKEN_KEY, token)
  if (user) localStorage.setItem(USER_KEY, JSON.stringify(user))
}

export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(USER_KEY)
}

// ---------- 实例 ----------

const baseURL = import.meta.env.VITE_API_BASE || '/api'

export const http = axios.create({ baseURL, timeout: 30000 })

http.interceptors.request.use((config) => {
  const token = getToken()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

http.interceptors.response.use(
  (response) => {
    // 二进制响应（截图）原样透传，交给 getBlob 处理。
    if (response.config.responseType === 'blob') {
      return response
    }
    const body = response.data
    if (body && typeof body === 'object' && 'code' in body) {
      if (body.code === 0) {
        return body.data
      }
      const message = body.message || '请求失败'
      toastError(message)
      return Promise.reject(new ApiError(body.code, message))
    }
    return body
  },
  (error) => {
    const response = error.response
    if (!response) {
      toastError('网络异常，请确认后端服务是否在运行')
      return Promise.reject(error)
    }
    const body = response.data
    const message = body?.message || `请求失败（HTTP ${response.status}）`
    if (response.status === 401) {
      // token 失效：清本地凭证并回登录页。用 location 而不是 router，
      // 是因为拦截器在 router 之外，直接跳转最简单、也不会产生循环依赖。
      clearAuth()
      if (window.location.pathname !== '/login') {
        window.location.assign('/login')
      }
    }
    toastError(message)
    return Promise.reject(new ApiError(body?.code ?? -1, message))
  },
)

// ---------- 便捷方法：返回值已剥掉外壳，即 data 本身 ----------

export const apiGet = (url, params, config) => http.get(url, { params, ...config })
export const apiPost = (url, data, config) => http.post(url, data, config)
export const apiPut = (url, data, config) => http.put(url, data, config)
export const apiDelete = (url, config) => http.delete(url, config)

/** 取二进制（任务截图）。返回 Blob。 */
export async function getBlob(url, config) {
  const response = await http.get(url, { responseType: 'blob', ...config })
  return response.data
}
