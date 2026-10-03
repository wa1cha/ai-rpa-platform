// 展示层映射：后端枚举字面量 → 中文文案 + 颜色 tone。
// 枚举的唯一出处是后端 core/enums.py，这里只是「怎么显示」，
// 不参与任何业务判断（判断一律看后端返回的原始值）。
// tone 对应 styles/index.css 里的 .tag--<tone>。

const ORDER_STATUS = {
  IMPORTED: { label: '已导入', tone: 'gray' },
  ANALYZING: { label: '分析中', tone: 'blue' },
  ANALYZED: { label: '已分析', tone: 'purple' },
  TASK_CREATED: { label: '已建任务', tone: 'cyan' },
  COMPLETED: { label: '已完成', tone: 'green' },
  FAILED: { label: '失败', tone: 'red' },
}

const TASK_STATUS = {
  PENDING: { label: '待处理', tone: 'gray' },
  WAITING_REVIEW: { label: '待审核', tone: 'orange' },
  QUEUED: { label: '已入队', tone: 'blue' },
  RUNNING: { label: '执行中', tone: 'cyan' },
  SUCCESS: { label: '成功', tone: 'green' },
  FAILED: { label: '失败', tone: 'red' },
  CANCELLED: { label: '已取消', tone: 'gray' },
}

const PRIORITY = {
  LOW: { label: '低', tone: 'gray' },
  MEDIUM: { label: '中', tone: 'blue' },
  HIGH: { label: '高', tone: 'red' },
}

const RISK = {
  LOW: { label: '低', tone: 'green' },
  MEDIUM: { label: '中', tone: 'orange' },
  HIGH: { label: '高', tone: 'red' },
}

const AI_STATUS = {
  SUCCESS: { label: '成功', tone: 'green' },
  FAILED: { label: '失败', tone: 'red' },
}

const REVIEW_RESULT = {
  APPROVED: { label: '已通过', tone: 'green' },
  REJECTED: { label: '已驳回', tone: 'red' },
}

const EXECUTION_STATUS = {
  RUNNING: { label: '执行中', tone: 'cyan' },
  SUCCESS: { label: '成功', tone: 'green' },
  FAILED: { label: '失败', tone: 'red' },
}

const IMPORT_BATCH_STATUS = {
  PROCESSING: { label: '处理中', tone: 'blue' },
  COMPLETED: { label: '已完成', tone: 'green' },
  FAILED: { label: '失败', tone: 'red' },
}

const NOTIFICATION_TYPE = {
  TASK_FAILED: { label: '任务失败', tone: 'red' },
  TASK_NEED_REVIEW: { label: '任务待审核', tone: 'orange' },
  IMPORT_FAILED: { label: '导入失败', tone: 'red' },
}

const NOTIFICATION_CHANNEL = {
  LOG: { label: '日志', tone: 'gray' },
  WECOM: { label: '企业微信', tone: 'blue' },
  DINGTALK: { label: '钉钉', tone: 'blue' },
  EMAIL: { label: '邮件', tone: 'blue' },
}

const NOTIFICATION_STATUS = {
  PENDING: { label: '待发送', tone: 'orange' },
  SENT: { label: '已发送', tone: 'green' },
  FAILED: { label: '发送失败', tone: 'red' },
}

const MAPS = {
  order: ORDER_STATUS,
  task: TASK_STATUS,
  priority: PRIORITY,
  risk: RISK,
  aiStatus: AI_STATUS,
  review: REVIEW_RESULT,
  execution: EXECUTION_STATUS,
  batch: IMPORT_BATCH_STATUS,
  notificationType: NOTIFICATION_TYPE,
  notificationChannel: NOTIFICATION_CHANNEL,
  notificationStatus: NOTIFICATION_STATUS,
}

/** 取某个枚举值的 {label, tone}，未知值原样回显、用灰色兜底。 */
export function enumMeta(kind, value) {
  if (value === null || value === undefined || value === '') {
    return { label: '-', tone: 'gray' }
  }
  const map = MAPS[kind] || {}
  return map[value] || { label: String(value), tone: 'gray' }
}

/** 直接取某种枚举的完整映射，供下拉筛选选项用。 */
export function enumOptions(kind) {
  return Object.entries(MAPS[kind] || {}).map(([value, meta]) => ({
    value,
    label: meta.label,
  }))
}

// ---------- 基础格式化 ----------

/** 后端时间已序列化为 "YYYY-MM-DD HH:mm:ss"，这里只做空值兜底。 */
export function formatDateTime(value) {
  if (!value) return '-'
  return String(value).replace('T', ' ')
}

export function formatDate(value) {
  if (!value) return '-'
  return String(value).slice(0, 10)
}

/** 金额后端是字符串（如 "299.00"），原样展示、不转 Number 以免丢尾零。 */
export function formatAmount(value) {
  if (value === null || value === undefined || value === '') return '-'
  return `¥${value}`
}

export function formatDeadline(value) {
  if (!value) return '-'
  if (value === 'TODAY') return '今天'
  if (value === 'TOMORROW') return '明天'
  if (value === 'NONE') return '无'
  return value
}

export function formatBool(value) {
  return value ? '是' : '否'
}

/** 执行耗时：后端存毫秒。 */
export function formatDuration(ms) {
  if (ms === null || ms === undefined || ms === '') return '-'
  if (ms < 1000) return `${ms} ms`
  return `${(ms / 1000).toFixed(2)} s`
}

/** 手机号：列表脱敏后端已做，详情页是全量，这里只给详情页补一个兜底。 */
export function formatPhone(value) {
  return value || '-'
}
