// 任务接口 —— 见《API接口设计》§9。返回值已被拦截器剥壳。
import { apiGet, apiPost, getBlob } from './request'

export const listTasks = (params) => apiGet('/v1/tasks', params)

export const getTask = (id) => apiGet(`/v1/tasks/${id}`)

export const listExecutions = (id) => apiGet(`/v1/tasks/${id}/executions`)

/** result 只能是 'APPROVED' | 'REJECTED'（后端枚举校验，非法值 422）。 */
export const reviewTask = (id, result, reason) =>
  apiPost(`/v1/tasks/${id}/review`, { result, reason: reason || null })

export const retryTask = (id, resetRetryCount, reason) =>
  apiPost(`/v1/tasks/${id}/retry`, {
    reset_retry_count: Boolean(resetRetryCount),
    reason: reason || null,
  })

export const cancelTask = (id, reason) =>
  apiPost(`/v1/tasks/${id}/cancel`, { reason: reason || null })

/**
 * 失败截图的二进制。**不能直接给 <img src>** —— 接口要 Bearer 头，
 * 浏览器贴 URL 不会带 Authorization。必须先取 Blob 再转 objectURL。
 */
export const fetchScreenshot = (taskId, executionId) =>
  getBlob(`/v1/tasks/${taskId}/executions/${executionId}/screenshot`)
