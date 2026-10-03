<script setup>
import { computed, onMounted, ref } from 'vue'

import { getSummary, getTrends } from '@/api/dashboard'
import { enumOptions, formatDateTime } from '@/utils/format'

const orderStatusOptions = enumOptions('order')
const taskStatusOptions = enumOptions('task')
const priorityOptions = enumOptions('priority')

const summary = ref(null)
const trends = ref([])
const trendDays = ref(7)
const TREND_CHOICES = [7, 14, 30]
const loading = ref(false)
const trendLoading = ref(false)

// 卡片数字在加载完成前显示占位，避免出现一闪而过的 0。
const stats = computed(() => {
  const s = summary.value
  if (!s) return []
  return [
    { label: '订单总数', value: s.orders.total },
    { label: '今日导入', value: s.orders.today },
    { label: '任务总数', value: s.tasks.total },
    { label: '待审核任务', value: s.tasks.by_status.WAITING_REVIEW },
    { label: '高风险订单', value: s.risk.high },
    { label: '需联系客户', value: s.risk.need_contact },
    { label: '在线 Worker', value: s.rpa.workers_online },
    { label: '最近任务成功', value: formatDateTime(s.rpa.last_success_at) },
  ]
})

async function loadSummary() {
  summary.value = await getSummary()
}

async function loadTrends() {
  trendLoading.value = true
  try {
    const data = await getTrends(trendDays.value)
    trends.value = data.days
  } finally {
    trendLoading.value = false
  }
}

async function loadAll() {
  loading.value = true
  try {
    await Promise.all([loadSummary(), loadTrends()])
  } finally {
    loading.value = false
  }
}

function onTrendDaysChange() {
  loadTrends()
}

onMounted(loadAll)
</script>

<template>
  <div class="page-head">
    <div class="page-head__title">看板</div>
    <button class="btn" :disabled="loading" @click="loadAll">
      {{ loading ? '加载中…' : '刷新' }}
    </button>
  </div>

  <div v-if="summary" class="stat-grid">
    <div v-for="s in stats" :key="s.label" class="stat">
      <div class="stat__label">{{ s.label }}</div>
      <div class="stat__value">{{ s.value }}</div>
    </div>
  </div>
  <div v-else class="card empty">加载中…</div>

  <div class="card">
    <div class="card__title">订单状态分布</div>
    <div class="row" style="flex-wrap: wrap; gap: 18px">
      <div v-for="o in orderStatusOptions" :key="o.value" class="row" style="gap: 6px">
        <span class="muted">{{ o.label }}</span>
        <strong>{{ summary ? summary.orders.by_status[o.value] : '-' }}</strong>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card__title">任务状态分布</div>
    <div class="row" style="flex-wrap: wrap; gap: 18px">
      <div v-for="o in taskStatusOptions" :key="o.value" class="row" style="gap: 6px">
        <span class="muted">{{ o.label }}</span>
        <strong>{{ summary ? summary.tasks.by_status[o.value] : '-' }}</strong>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card__title">任务优先级分布</div>
    <div class="row" style="flex-wrap: wrap; gap: 18px">
      <div v-for="o in priorityOptions" :key="o.value" class="row" style="gap: 6px">
        <span class="muted">{{ o.label }}</span>
        <strong>{{ summary ? summary.tasks.by_priority[o.value] : '-' }}</strong>
      </div>
    </div>
  </div>

  <div class="card">
    <div class="page-head" style="margin-bottom: 12px">
      <div class="card__title" style="margin-bottom: 0">趋势</div>
      <div class="row">
        <span class="muted">近</span>
        <select v-model.number="trendDays" class="select" style="width: 90px" @change="onTrendDaysChange">
          <option v-for="d in TREND_CHOICES" :key="d" :value="d">{{ d }} 天</option>
        </select>
      </div>
    </div>

    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>日期</th>
            <th>导入</th>
            <th>成功</th>
            <th>失败</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in trends" :key="row.date">
            <td>{{ row.date }}</td>
            <td>{{ row.imported }}</td>
            <td style="color: var(--c-green)">{{ row.success }}</td>
            <td style="color: var(--c-red)">{{ row.failed }}</td>
          </tr>
          <tr v-if="!trendLoading && trends.length === 0">
            <td colspan="4" class="empty">暂无数据</td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="muted" style="margin-top: 10px; font-size: 12.5px">
      成功/失败按任务**到达终态那天**归档；系统不保留状态变更历史，重试不单独体现。
    </div>
  </div>
</template>
