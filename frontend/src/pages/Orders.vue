<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { importOrders, listOrders } from '@/api/orders'
import Pagination from '@/components/Pagination.vue'
import PriorityTag from '@/components/PriorityTag.vue'
import RiskTag from '@/components/RiskTag.vue'
import StatusTag from '@/components/StatusTag.vue'
import { enumOptions, formatAmount, formatDateTime } from '@/utils/format'
import { toastSuccess } from '@/utils/toast'

const router = useRouter()

const statusOptions = enumOptions('order')

const filters = reactive({
  statuses: [],
  order_no: '',
  customer_name: '',
  has_risk: false,
  start_date: '',
  end_date: '',
})

const rows = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)

// 只把有值的筛选项发给后端 —— 空串会被当成「按空字符串匹配」，
// 结果永远查不到数据（后端把空串当有效值）。
function buildParams() {
  const params = { page: page.value, page_size: pageSize.value }
  if (filters.statuses.length) params.status = filters.statuses.join(',')
  if (filters.order_no) params.order_no = filters.order_no
  if (filters.customer_name) params.customer_name = filters.customer_name
  if (filters.has_risk) params.has_risk = true
  if (filters.start_date) params.start_date = filters.start_date
  if (filters.end_date) params.end_date = filters.end_date
  return params
}

async function load() {
  loading.value = true
  try {
    const data = await listOrders(buildParams())
    rows.value = data.items
    total.value = data.total
  } finally {
    loading.value = false
  }
}

// 改筛选条件时回到第一页，否则可能停在一个已经不存在的页码上。
function search() {
  page.value = 1
  load()
}

function reset() {
  filters.statuses = []
  filters.order_no = ''
  filters.customer_name = ''
  filters.has_risk = false
  filters.start_date = ''
  filters.end_date = ''
  search()
}

function onPageChange(target) {
  page.value = target
  load()
}

function openDetail(row) {
  router.push({ name: 'order-detail', params: { id: row.id } })
}

// ---------- 导入 ----------

const importVisible = ref(false)
const importFile = ref(null)
const dryRun = ref(false)
const importing = ref(false)
const importResult = ref(null)

function openImport() {
  importFile.value = null
  dryRun.value = false
  importResult.value = null
  importVisible.value = true
}

function closeImport() {
  importVisible.value = false
  // 导入成功过（非 dry-run）就刷新列表，让新单立刻可见。
  if (importResult.value && !dryRun.value && importResult.value.success_rows > 0) {
    search()
  }
}

function onFileChange(event) {
  importFile.value = event.target.files?.[0] || null
  importResult.value = null
}

async function submitImport() {
  if (!importFile.value) return
  importing.value = true
  try {
    importResult.value = await importOrders(importFile.value, dryRun.value)
    const r = importResult.value
    if (r.status === 'COMPLETED') {
      toastSuccess(dryRun.value ? `校验通过 ${r.success_rows} 行` : `成功导入 ${r.success_rows} 行`)
    }
    // 失败（整批零插入）不弹成功提示 —— 错误明细就在下面的表格里。
  } finally {
    importing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page-head">
    <div class="page-head__title">订单</div>
    <button class="btn btn--primary" @click="openImport">导入订单</button>
  </div>

  <div class="card">
    <div class="filter-bar">
      <div class="field filter-bar__item" style="min-width: 320px">
        <label class="field__label">状态</label>
        <div class="chip-group">
          <label v-for="opt in statusOptions" :key="opt.value" class="chip">
            <input v-model="filters.statuses" type="checkbox" :value="opt.value" />
            {{ opt.label }}
          </label>
        </div>
      </div>
      <div class="field filter-bar__item">
        <label class="field__label">订单号</label>
        <input v-model.trim="filters.order_no" class="input" @keyup.enter="search" />
      </div>
      <div class="field filter-bar__item">
        <label class="field__label">客户名称</label>
        <input v-model.trim="filters.customer_name" class="input" @keyup.enter="search" />
      </div>
      <div class="field filter-bar__item">
        <label class="field__label">下单开始</label>
        <input v-model="filters.start_date" class="input" type="date" />
      </div>
      <div class="field filter-bar__item">
        <label class="field__label">下单结束</label>
        <input v-model="filters.end_date" class="input" type="date" />
      </div>
      <div class="field">
        <label class="field__label">仅风险单</label>
        <label class="chip">
          <input v-model="filters.has_risk" type="checkbox" />
          只看有风险的订单
        </label>
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
            <th>订单号</th>
            <th>平台</th>
            <th>客户</th>
            <th>手机号</th>
            <th>商品</th>
            <th>数量</th>
            <th>金额</th>
            <th>状态</th>
            <th>风险</th>
            <th>优先级</th>
            <th>任务</th>
            <th>下单时间</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.id">
            <td>{{ row.order_no }}</td>
            <td>{{ row.platform }}</td>
            <td>{{ row.customer_name }}</td>
            <td>{{ row.phone }}</td>
            <td>
              {{ row.product_name }}
              <div class="faint" style="font-size: 12px">{{ row.sku }}</div>
            </td>
            <td>{{ row.quantity }}</td>
            <td>{{ formatAmount(row.amount) }}</td>
            <td><StatusTag kind="order" :value="row.status" /></td>
            <td><RiskTag :value="row.risk_level" /></td>
            <td><PriorityTag :value="row.priority" /></td>
            <td>
              <StatusTag v-if="row.task_status" kind="task" :value="row.task_status" />
              <span v-else class="faint">-</span>
            </td>
            <td>{{ formatDateTime(row.ordered_at) }}</td>
            <td>
              <button class="btn btn--sm" @click="openDetail(row)">详情</button>
            </td>
          </tr>
          <tr v-if="!loading && rows.length === 0">
            <td colspan="13" class="empty">没有符合条件的订单</td>
          </tr>
        </tbody>
      </table>
    </div>

    <Pagination
      :page="page"
      :page-size="pageSize"
      :total="total"
      @change="onPageChange"
    />
  </div>

  <div v-if="importVisible" class="modal-mask" @click.self="closeImport">
    <div class="modal" style="width: 600px">
      <div class="modal__head">导入订单</div>
      <div class="modal__body">
        <div class="field">
          <label class="field__label">选择文件（.xlsx / .xlsm / .csv，最大 10 MB）</label>
          <input type="file" accept=".xlsx,.xlsm,.csv" @change="onFileChange" />
        </div>

        <label class="chip">
          <input v-model="dryRun" type="checkbox" />
          仅校验（dry-run，不写入数据库）
        </label>

        <div v-if="importResult" class="card" style="margin: 16px 0 0">
          <div class="card__title">
            结果：
            <span :class="importResult.status === 'COMPLETED' ? 'muted' : ''">
              {{ importResult.status === 'COMPLETED' ? '成功' : '失败（整批未插入）' }}
            </span>
          </div>
          <div class="desc" style="grid-template-columns: repeat(3, 1fr)">
            <div class="desc__item"><span class="desc__label">总行数</span>{{ importResult.total_rows }}</div>
            <div class="desc__item"><span class="desc__label">成功</span>{{ importResult.success_rows }}</div>
            <div class="desc__item"><span class="desc__label">失败</span>{{ importResult.failed_rows }}</div>
          </div>

          <div v-if="importResult.errors.length" class="table-wrap" style="margin-top: 12px">
            <table class="table">
              <thead>
                <tr>
                  <th>行号</th>
                  <th>订单号</th>
                  <th>原因</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(err, index) in importResult.errors" :key="index">
                  <td>{{ err.row }}</td>
                  <td>{{ err.order_no ?? '-' }}</td>
                  <td style="color: var(--c-red)">{{ err.reason }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>
      <div class="modal__foot">
        <button class="btn" :disabled="importing" @click="closeImport">关闭</button>
        <button
          class="btn btn--primary"
          :disabled="!importFile || importing"
          @click="submitImport"
        >
          {{ importing ? '处理中…' : dryRun ? '校验' : '导入' }}
        </button>
      </div>
    </div>
  </div>
</template>
