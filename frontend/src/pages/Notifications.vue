<script setup>
import { onMounted, reactive, ref } from 'vue'

import { listNotifications } from '@/api/notifications'
import Pagination from '@/components/Pagination.vue'
import StatusTag from '@/components/StatusTag.vue'
import { enumOptions, formatDateTime } from '@/utils/format'

// v1 通知只有 LOG 渠道、写下即 SENT，所以**默认不带 status 过滤**才看得到流水；
// 「待发送」是留给以后真实异步渠道的初始态，现在筛它会是空的（符合事实）。
const statusOptions = enumOptions('notificationStatus')

const filters = reactive({ status: '' })
const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)

async function load() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filters.status) params.status = filters.status
    const data = await listNotifications(params)
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
  filters.status = ''
  search()
}

function onPageChange(target) {
  page.value = target
  load()
}

onMounted(load)
</script>

<template>
  <div class="page-head">
    <div class="page-head__title">通知</div>
  </div>

  <div class="card">
    <div class="filter-bar">
      <div class="field filter-bar__item">
        <label class="field__label">状态</label>
        <select v-model="filters.status" class="select">
          <option value="">全部</option>
          <option v-for="o in statusOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
        </select>
      </div>
      <button class="btn btn--primary" @click="search">查询</button>
      <button class="btn" @click="reset">重置</button>
    </div>

    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>ID</th>
            <th>类型</th>
            <th>渠道</th>
            <th>标题</th>
            <th>内容</th>
            <th>状态</th>
            <th>关联任务</th>
            <th>创建时间</th>
            <th>发送时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id">
            <td>{{ row.id }}</td>
            <td><StatusTag kind="notificationType" :value="row.type" /></td>
            <td><StatusTag kind="notificationChannel" :value="row.channel" /></td>
            <td>{{ row.title || '-' }}</td>
            <td :title="row.content || ''" style="max-width: 320px">{{ row.content || '-' }}</td>
            <td><StatusTag kind="notificationStatus" :value="row.status" /></td>
            <td>
              <router-link
                v-if="row.related_task_id"
                :to="{ name: 'task-detail', params: { id: row.related_task_id } }"
              >
                #{{ row.related_task_id }}
              </router-link>
              <span v-else>-</span>
            </td>
            <td>{{ formatDateTime(row.created_at) }}</td>
            <td>{{ formatDateTime(row.sent_at) }}</td>
          </tr>
          <tr v-if="!loading && rows.length === 0">
            <td colspan="9" class="empty">还没有通知</td>
          </tr>
        </tbody>
      </table>
    </div>

    <Pagination :page="page" :page-size="pageSize" :total="total" @change="onPageChange" />
  </div>
</template>
