<script setup>
import { onMounted, reactive, ref } from 'vue'

import { getBatch, listBatches } from '@/api/importBatches'
import Pagination from '@/components/Pagination.vue'
import StatusTag from '@/components/StatusTag.vue'
import { enumOptions, formatDateTime } from '@/utils/format'

const statusOptions = enumOptions('batch')

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
    const data = await listBatches(params)
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

function onPageChange(target) {
  page.value = target
  load()
}

// ---------- 详情 ----------

const detailVisible = ref(false)
const detail = ref(null)
const detailLoading = ref(false)

async function openDetail(row) {
  detailVisible.value = true
  detailLoading.value = true
  detail.value = null
  try {
    // 列表不返回错误明细（一次可能几百条），详情才拉。
    detail.value = await getBatch(row.id)
  } finally {
    detailLoading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page-head">
    <div class="page-head__title">导入批次</div>
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
    </div>

    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>批次号</th>
            <th>文件名</th>
            <th>上传人</th>
            <th>总行数</th>
            <th>成功</th>
            <th>失败</th>
            <th>状态</th>
            <th>创建时间</th>
            <th>完成时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id">
            <td>#{{ row.id }}</td>
            <td>{{ row.filename }}</td>
            <td>{{ row.uploaded_by || '-' }}</td>
            <td>{{ row.total_rows }}</td>
            <td>{{ row.success_rows }}</td>
            <td>{{ row.failed_rows }}</td>
            <td><StatusTag kind="batch" :value="row.status" /></td>
            <td>{{ formatDateTime(row.created_at) }}</td>
            <td>{{ formatDateTime(row.finished_at) }}</td>
            <td>
              <button class="btn btn--sm" @click="openDetail(row)">详情</button>
            </td>
          </tr>
          <tr v-if="!loading && rows.length === 0">
            <td colspan="10" class="empty">还没有导入批次</td>
          </tr>
        </tbody>
      </table>
    </div>

    <Pagination :page="page" :page-size="pageSize" :total="total" @change="onPageChange" />
  </div>

  <div v-if="detailVisible" class="modal-mask" @click.self="detailVisible = false">
    <div class="modal" style="width: 640px">
      <div class="modal__head">批次详情</div>
      <div class="modal__body">
        <div v-if="detailLoading" class="empty">加载中…</div>
        <template v-else-if="detail">
          <div class="desc" style="grid-template-columns: repeat(2, 1fr)">
            <div class="desc__item"><span class="desc__label">批次号</span>#{{ detail.id }}</div>
            <div class="desc__item"><span class="desc__label">文件</span>{{ detail.filename }}</div>
            <div class="desc__item"><span class="desc__label">状态</span><StatusTag kind="batch" :value="detail.status" /></div>
            <div class="desc__item"><span class="desc__label">上传人</span>{{ detail.uploaded_by || '-' }}</div>
            <div class="desc__item"><span class="desc__label">总行数</span>{{ detail.total_rows }}</div>
            <div class="desc__item"><span class="desc__label">成功/失败</span>{{ detail.success_rows }} / {{ detail.failed_rows }}</div>
            <div class="desc__item"><span class="desc__label">创建</span>{{ formatDateTime(detail.created_at) }}</div>
            <div class="desc__item"><span class="desc__label">完成</span>{{ formatDateTime(detail.finished_at) }}</div>
          </div>

          <div v-if="detail.errors.length" style="margin-top: 14px">
            <div class="card__title">失败明细（{{ detail.errors.length }} 条）</div>
            <div class="table-wrap" style="max-height: 260px; overflow: auto">
              <table class="table">
                <thead>
                  <tr>
                    <th>行号</th>
                    <th>订单号</th>
                    <th>原因</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(err, index) in detail.errors" :key="index">
                    <td>{{ err.row }}</td>
                    <td>{{ err.order_no ?? '-' }}</td>
                    <td style="color: var(--c-red)">{{ err.reason }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <div v-if="detail.order_ids.length" style="margin-top: 14px">
            <div class="card__title">关联订单（{{ detail.order_ids.length }}）</div>
            <div class="row" style="flex-wrap: wrap; gap: 8px">
              <router-link
                v-for="oid in detail.order_ids"
                :key="oid"
                :to="{ name: 'order-detail', params: { id: oid } }"
              >
                <span class="tag tag--blue">#{{ oid }}</span>
              </router-link>
            </div>
          </div>
          <div v-else-if="!detail.errors.length" class="empty" style="padding: 16px 0">
            该批次没有失败明细
          </div>
        </template>
      </div>
      <div class="modal__foot">
        <button class="btn" @click="detailVisible = false">关闭</button>
      </div>
    </div>
  </div>
</template>
