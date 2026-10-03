// 认证接口 —— 见《API接口设计》§5。
// 返回值已被 request.js 的拦截器剥壳，即 {access_token, token_type, expires_in, user}。
import { apiGet, apiPost } from './request'

export const login = (username, password) =>
  apiPost('/v1/auth/login', { username, password })

export const me = () => apiGet('/v1/auth/me')

export const logout = () => apiPost('/v1/auth/logout')
