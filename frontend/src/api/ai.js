// AI 分析接口 —— 见《API接口设计》§8。返回值已被拦截器剥壳。
import { apiGet, apiPost } from './request'

export const listAnalyses = (params) => apiGet('/v1/ai-analyses', params)

/**
 * 人工修正。changes 形如 [{ field_name: 'priority', new_value: 'HIGH' }]，
 * new_value **一律是字符串** —— 布尔/枚举的解析由后端按字段类型负责。
 */
export const reviewAnalysis = (id, changes, reason) =>
  apiPost(`/v1/ai-analyses/${id}/review`, { changes, reason: reason || null })
