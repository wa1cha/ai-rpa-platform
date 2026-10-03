// 订单接口 —— 见《API接口设计》§6。返回值已被拦截器剥壳。
import { apiGet, apiPost } from './request'

export const listOrders = (params) => apiGet('/v1/orders', params)

export const getOrder = (id) => apiGet(`/v1/orders/${id}`)

/**
 * 导入订单。file 是浏览器 File 对象，走 multipart。
 * 不手动设 Content-Type —— 让 axios 自己带上 boundary。
 */
export function importOrders(file, dryRun) {
  const form = new FormData()
  form.append('file', file)
  form.append('dry_run', dryRun ? 'true' : 'false')
  return apiPost('/v1/orders/import', form)
}

/** 重新触发 AI 分析。不带 body 也合法，所以 reason 为空时发空对象。 */
export const reanalyzeOrder = (id, reason) =>
  apiPost(`/v1/orders/${id}/reanalyze`, reason ? { reason } : {})
