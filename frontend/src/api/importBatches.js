// 导入批次接口 —— 见《API接口设计》§7。
//
// 计划里 API 层只列了 6 个文件，但导入批次也要一个去处；放进 ai.js 或 orders.js
// 都名不副实，所以单开一个（Phase 6 的唯一一处目录偏离）。
import { apiGet } from './request'

export const listBatches = (params) => apiGet('/v1/import-batches', params)

export const getBatch = (id) => apiGet(`/v1/import-batches/${id}`)
