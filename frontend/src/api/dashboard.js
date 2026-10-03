// 看板接口 —— 见《API接口设计》§11。返回值已被拦截器剥壳。
import { apiGet } from './request'

export const getSummary = () => apiGet('/v1/dashboard/summary')

export const getTrends = (days = 7) => apiGet('/v1/dashboard/trends', { days })
