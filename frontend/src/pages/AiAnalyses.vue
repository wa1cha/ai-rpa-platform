<script setup>
import { computed, onMounted, reactive, ref } from 'vue'

import { listAnalyses, reviewAnalysis } from '@/api/ai'
import Pagination from '@/components/Pagination.vue'
import PriorityTag from '@/components/PriorityTag.vue'
import RiskTag from '@/components/RiskTag.vue'
import StatusTag from '@/components/StatusTag.vue'
import {
  enumOptions,
  formatBool,
  formatDateTime,
  formatDeadline,
} from '@/utils/format'
import { toastSuccess } from '@/utils/toast'

const riskOptions = enumOptions('risk')
const priorityOptions = enumOptions('priority')
const aiStatusOptions = enumOptions('aiStatus')

// 可修正字段 —— 与后端 ai_service._ALLOWED_REVIEW_FIELDS 一致（6 个）。
// 「怎么输入这个字段」是纯展示问题，选择器形态各字段不同所以在这里声明。
const EDITABLE_FIELDS = [
  { value: 'priority', label: '优先级', kind: 'select', options: priorityOptions },
  { value: 'risk_level', label: '风险等级', kind: 'select', options: riskOptions },
  {
    value: 'deadline',
    label: '交期',
    kind: 'text',
    placeholder: 'TODAY / TOMORROW / NONE，或具体日期 YYYY-MM-DD',
  },
  {
    value: 'need_contact',
    label: '需联系客户',
    kind: 'select',
    options: [
      { value: 'true', label: '是' },
      { value: 'false', label: '否' },
    ],
  },
  { value: 'risk_reason', label: '风险原因', kind: 'textarea' },
  { value: 'action', label: '建议动作', kind: 'textarea' },
]

const filters = reactive({
  risk_level: '',
  priority: '',
  need_contact: '',
  status: '',
  order_no: '',
})

const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)

function buildParams() {
  const params = { page: page.value, page_size: pageSize.value }
  if (filters.risk_level) params.risk_level = filters.risk_level
  if (filters.priority) params.priority = filters.priority
  if (filters.need_contact) params.need_contact = filters.need_contact === 'true'
  if (filters.status) params.status = filters.status
  if (filters.order_no) params.order_no = filters.order_no
  return params
}

async function load() {
  loading.value = true
  try {
    const data = await listAnalyses(buildParams())
    rows.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

function search() {
  page.value = 1
  load()
}

function reset() {
  filters.risk_level = ''
  filters.priority = ''
  filters.need_contact = ''
  filters.status = ''
  filters.order_no = ''
  search()
}

function onPageChange(target) {
  page.value = target
  load()
}

// ---------- 修正 ----------

const correctVisible = ref(false)
const correctRow = ref(null)
const correctField = ref('priority')
const correctValue = ref('')
const correctReason = ref('')
const correcting = ref(false)

const fieldSpec = computed(
  () => EDITABLE_FIELDS.find((f) => f.value === correctField.value) || EDITABLE_FIELDS[0],
)

/** 把库里当前值转成输入框里的字符串（布尔转 "true"/"false"，与后端契约一致）。 */
function toInputValue(row, field) {
  const value = row?.[field]
  if (value === null || value === undefined) return ''
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  return String(value)
}

function openCorrect(row) {
  correctRow.value = row
  correctField.value = 'priority'
  correctValue.value = toInputValue(row, 'priority')
  correctReason.value = ''
  correctVisible.value = true
}

function onFieldChange() {
  correctValue.value = toInputValue(correctRow.value, correctField.value)
}

async function submitCorrect() {
  if (!correctValue.value.trim()) return
  correcting.value = true
  try {
    const result = await reviewAnalysis(
      correctRow.value.id,
      [{ field_name: correctField.value, new_value: correctValue.value.trim() }],
      correctReason.value,
    )
    correctVisible.value = false
    const applied = result.applied[0]
    toastSuccess(
      `已把 ${applied.field_name} 改为 ${applied.new_value}` +
        (result.task_updated ? '（任务优先级已同步）' : ''),
    )
    await load()
  } finally {
    correcting.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page-head">
    <div class="page-head__title">AI 分析</div>
  </div>

  <div class="card">
    <div class="filter-bar">
      <div class="field filter-bar__item">
        <label class="field__label">风险等级</label>
        <select v-model="filters.risk_level" class="select">
          <option value="">全部</option>
          <option v-for="o in riskOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
        </select>
      </div>
      <div class="field filter-bar__item">
        <label class="field__label">优先级</label>
        <select v-model="filters.priority" class="select">
          <option value="">全部</option>
          <option v-for="o in priorityOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
        </select>
      </div>
      <div class="field filter-bar__item">
        <label class="field__label">需联系客户</label>
        <select v-model="filters.need_contact" class="select">
          <option value="">全部</option>
          <option value="true">是</option>
          <option value="false">否</option>
        </select>
      </div>
      <div class="field filter-bar__item">
        <label class="field__label">分析状态</label>
        <select v-model="filters.status" class="select">
          <option value="">全部</option>
          <option v-for="o in aiStatusOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
        </select>
      </div>
      <div class="field filter-bar__item">
        <label class="field__label">订单号</label>
        <input v-model.trim="filters.order_no" class="input" @keyup.enter="search" />
      </div>
      <div class="row">
        <button class="btn btn--primary" @click="search">查询</button>
        <button class="btn" @click="reset">重置</button>
      </div>
    </div>

    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>ID</th>
            <th>订单号</th>
            <th>优先级</th>
            <th>交期</th>
            <th>需联系</th>
            <th>风险</th>
            <th>风险原因</th>
            <th>建议动作</th>
            <th>模型</th>
            <th>状态</th>
            <th>分析时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id">
            <td>{{ row.id }}</td>
            <td>{{ row.order_no }}</td>
            <td><PriorityTag :value="row.priority" /></td>
            <td>{{ formatDeadline(row.deadline) }}</td>
            <td>{{ formatBool(row.need_contact) }}</td>
            <td><RiskTag :value="row.risk_level" /></td>
            <td :title="row.risk_reason || ''" style="max-width: 220px">{{ row.risk_reason || '-' }}</td>
            <td :title="row.action || ''" style="max-width: 220px">{{ row.action || '-' }}</td>
            <td>{{ row.model_name || '-' }}</td>
            <td><StatusTag kind="aiStatus" :value="row.status" /></td>
            <td>{{ formatDateTime(row.created_at) }}</td>
            <td>
              <button class="btn btn--sm" @click="openCorrect(row)">修正</button>
            </td>
          </tr>
          <tr v-if="!loading && rows.length === 0">
            <td colspan="12" class="empty">没有符合条件的分析记录</td>
          </tr>
        </tbody>
      </table>
    </div>

    <Pagination :page="page" :page-size="pageSize" :total="total" @change="onPageChange" />
  </div>

  <div v-if="correctVisible" class="modal-mask" @click.self="correctVisible = false">
    <div class="modal">
      <div class="modal__head">修正 AI 分析 #{{ correctRow?.id }}</div>
      <div class="modal__body">
        <div class="field">
          <label class="field__label">字段</label>
          <select v-model="correctField" class="select" @change="onFieldChange">
            <option v-for="f in EDITABLE_FIELDS" :key="f.value" :value="f.value">
              {{ f.label }}
            </option>
          </select>
        </div>

        <div class="field">
          <label class="field__label">
            新值
            <span style="color: var(--c-text-faint)">
              （当前：{{ toInputValue(correctRow, correctField) || '空' }}）
            </span>
          </label>
          <select v-if="fieldSpec.kind === 'select'" v-model="correctValue" class="select">
            <option v-for="o in fieldSpec.options" :key="o.value" :value="o.value">
              {{ o.label }}
            </option>
          </select>
          <textarea
            v-else-if="fieldSpec.kind === 'textarea'"
            v-model="correctValue"
            class="textarea"
            rows="3"
            maxlength="500"
          ></textarea>
          <input
            v-else
            v-model.trim="correctValue"
            class="input"
            :placeholder="fieldSpec.placeholder"
          />
        </div>

        <div class="field" style="margin-bottom: 0">
          <label class="field__label">修正原因（选填，仅记日志）</label>
          <textarea v-model="correctReason" class="textarea" rows="2" maxlength="500"></textarea>
        </div>
      </div>
      <div class="modal__foot">
        <button class="btn" :disabled="correcting" @click="correctVisible = false">取消</button>
        <button
          class="btn btn--primary"
          :disabled="correcting || !correctValue.trim()"
          @click="submitCorrect"
        >
          {{ correcting ? '提交中…' : '确认修正' }}
        </button>
      </div>
    </div>
  </div>
</template>
