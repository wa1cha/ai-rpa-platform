// 通知接口 —— 见《API接口设计》§12。
//
// 计划里的 API 层只列了 6 个文件、没有通知的去处；但通知页要数据源，
// 塞进 dashboard.js 名不副实，所以与 importBatches.js 同理单开一个。
import { apiGet } from './request'

export const listNotifications = (params) => apiGet('/v1/notifications', params)
