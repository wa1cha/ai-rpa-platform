<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { listTasks } from '@/api/tasks'
import Pagination from '@/components/Pagination.vue'
import PriorityTag from '@/components/PriorityTag.vue'
import StatusTag from '@/components/StatusTag.vue'
import { enumOptions, formatDateTime } from '@/utils/format'

const router = useRouter()

const statusOptions = enumOptions('task')
const priorityOptions = enumOptions('priority')

const filters = reactive({ statuses: [], priority: '' })

const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)

function buildParams() {
  const params = { page: page.value, page_size: pageSize.value }
  if (filters.statuses.length) params.status = filters.statuses.join(',')
  if (filters.priority) params.priority = filters.priority
  return params
}

async function load() {
  loading.value = true
  try {
    const data = await listTasks(buildParams())
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
  filters.statuses = []
  filters.priority = ''
  search()
}

function onPageChange(target) {
  page.value = target
  load()
}

function openDetail(row) {
  router.push({ name: 'task-detail', params: { id: row.id } })
}

onMounted(load)
</script>

<template>
  <div class="page-head">
    <div class="page-head__title">任务</div>
  </div>

  <div class="card">
    <div class="filter-bar">
      <div class="field filter-bar__item" style="min-width: 380px">
        <label class="field__label">状态</label>
        <div class="chip-group">
          <label v-for="opt in statusOptions" :key="opt.value" class="chip">
            <input v-model="filters.statuses" type="checkbox" :value="opt.value" />
            {{ opt.label }}
          </label>
        </div>
      </div>
      <div class="field filter-bar__item">
        <label class="field__label">优先级</label>
        <select v-model="filters.priority" class="select">
          <option value="">全部</option>
          <option v-for="opt in priorityOptions" :key="opt.value" :value="opt.value">
            {{ opt.label }}
          </option>
        </select>
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
            <th>任务号</th>
            <th>订单号</th>
            <th>客户</th>
            <th>优先级</th>
            <th>状态</th>
            <th>需审核</th>
            <th>重试</th>
            <th>执行人</th>
            <th>最近错误</th>
            <th>创建时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id">
            <td>#{{ row.id }}</td>
            <td>{{ row.order_no }}</td>
            <td>{{ row.customer_name }}</td>
            <td><PriorityTag :value="row.priority" /></td>
            <td><StatusTag kind="task" :value="row.status" /></td>
            <td>{{ row.need_review ? '是' : '否' }}</td>
            <td>{{ row.retry_count }} / {{ row.max_retry }}</td>
            <td>{{ row.claimed_by || '-' }}</td>
            <td :title="row.last_error || ''" style="max-width: 200px; overflow: hidden; text-overflow: ellipsis">
              {{ row.last_error || '-' }}
            </td>
            <td>{{ formatDateTime(row.created_at) }}</td>
            <td>
              <button class="btn btn--sm" @click="openDetail(row)">详情</button>
            </td>
          </tr>
          <tr v-if="!loading && rows.length === 0">
            <td colspan="11" class="empty">没有符合条件的任务</td>
          </tr>
        </tbody>
      </table>
    </div>

    <Pagination :page="page" :page-size="pageSize" :total="total" @change="onPageChange" />
  </div>
</template>
