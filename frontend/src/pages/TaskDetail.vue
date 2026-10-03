<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  cancelTask,
  fetchScreenshot,
  getTask,
  retryTask,
  reviewTask,
} from '@/api/tasks'
import PriorityTag from '@/components/PriorityTag.vue'
import ReviewDialog from '@/components/ReviewDialog.vue'
import RiskTag from '@/components/RiskTag.vue'
import StatusTag from '@/components/StatusTag.vue'
import {
  enumMeta,
  formatAmount,
  formatBool,
  formatDateTime,
  formatDeadline,
  formatDuration,
} from '@/utils/format'
import { toastSuccess } from '@/utils/toast'

const route = useRoute()
const router = useRouter()

const task = ref(null)
const loading = ref(true)
const acting = ref(false)

async function load() {
  loading.value = true
  try {
    task.value = await getTask(route.params.id)
  } finally {
    loading.value = false
  }
}

// 状态决定哪些按钮可用。后端才是权威（越权一律 409/4009），
// 这里只是不给用户点了必然失败的按钮。
const status = computed(() => task.value?.status)
const canReview = computed(() => status.value === 'WAITING_REVIEW')
const canRetry = computed(() => status.value === 'FAILED')
const canCancel = computed(() =>
  ['PENDING', 'WAITING_REVIEW', 'QUEUED'].includes(status.value),
)

// ---------- 审核 ----------

const reviewVisible = ref(false)
const reviewResult = ref('APPROVED')

const reviewDialogProps = computed(() =>
  reviewResult.value === 'APPROVED'
    ? { title: '通过审核', reasonLabel: '审核意见', confirmText: '确认通过', danger: false }
    : { title: '驳回任务', reasonLabel: '驳回原因', confirmText: '确认驳回', danger: true },
)

function openReview(result) {
  reviewResult.value = result
  reviewVisible.value = true
}

async function confirmReview({ reason }) {
  acting.value = true
  try {
    await reviewTask(task.value.id, reviewResult.value, reason)
    reviewVisible.value = false
    toastSuccess(reviewResult.value === 'APPROVED' ? '已通过，任务重新入队' : '已驳回，任务取消')
    await load()
  } finally {
    acting.value = false
  }
}

// ---------- 重试 ----------

const retryVisible = ref(false)

async function confirmRetry({ reason, checked }) {
  acting.value = true
  try {
    await retryTask(task.value.id, checked, reason)
    retryVisible.value = false
    toastSuccess('已重新入队')
    await load()
  } finally {
    acting.value = false
  }
}

// ---------- 取消 ----------

const cancelVisible = ref(false)

async function confirmCancel({ reason }) {
  acting.value = true
  try {
    await cancelTask(task.value.id, reason)
    cancelVisible.value = false
    toastSuccess('任务已取消')
    await load()
  } finally {
    acting.value = false
  }
}

// ---------- 截图 ----------

const shotVisible = ref(false)
const shotUrl = ref('')
const shotError = ref('')
const shotLoading = ref(false)

async function viewScreenshot(execution) {
  shotVisible.value = true
  shotLoading.value = true
  shotError.value = ''
  shotUrl.value = ''
  try {
    const blob = await fetchScreenshot(task.value.id, execution.id)
    shotUrl.value = URL.createObjectURL(blob)
  } catch (err) {
    shotError.value = err?.message || '截图读取失败'
  } finally {
    shotLoading.value = false
  }
}

function closeScreenshot() {
  shotVisible.value = false
  if (shotUrl.value) {
    URL.revokeObjectURL(shotUrl.value)
    shotUrl.value = ''
  }
}

onMounted(load)
</script>

<template>
  <div class="page-head">
    <button class="btn btn--sm" @click="router.back()">← 返回</button>
    <div class="row" v-if="task">
      <button v-if="canReview" class="btn btn--primary" @click="openReview('APPROVED')">
        通过审核
      </button>
      <button v-if="canReview" class="btn btn--danger" @click="openReview('REJECTED')">
        驳回
      </button>
      <button v-if="canRetry" class="btn btn--primary" @click="retryVisible = true">重试</button>
      <button v-if="canCancel" class="btn btn--danger" @click="cancelVisible = true">取消</button>
      <StatusTag kind="task" :value="task.status" />
    </div>
  </div>

  <div v-if="loading" class="card empty">加载中…</div>

  <template v-else-if="task">
    <div class="card">
      <div class="card__title">任务信息</div>
      <div class="desc">
        <div class="desc__item"><span class="desc__label">任务号</span>#{{ task.id }}</div>
        <div class="desc__item">
          <span class="desc__label">优先级</span><PriorityTag :value="task.priority" />
        </div>
        <div class="desc__item"><span class="desc__label">需审核</span>{{ formatBool(task.need_review) }}</div>
        <div class="desc__item">
          <span class="desc__label">重试</span>{{ task.retry_count }} / {{ task.max_retry }}
        </div>
        <div class="desc__item"><span class="desc__label">执行人</span>{{ task.claimed_by || '-' }}</div>
        <div class="desc__item"><span class="desc__label">入队时间</span>{{ formatDateTime(task.queued_at) }}</div>
        <div class="desc__item"><span class="desc__label">完成时间</span>{{ formatDateTime(task.finished_at) }}</div>
        <div class="desc__item" v-if="task.review_result">
          <span class="desc__label">审核结果</span>
          <span class="tag" :class="`tag--${enumMeta('review', task.review_result).tone}`">
            {{ enumMeta('review', task.review_result).label }}
          </span>
        </div>
        <div class="desc__item" v-if="task.review_reason">
          <span class="desc__label">审核意见</span>{{ task.review_reason }}
        </div>
        <div class="desc__item" v-if="task.cancel_reason">
          <span class="desc__label">取消原因</span>{{ task.cancel_reason }}
        </div>
        <div class="desc__item" style="grid-column: 1 / -1" v-if="task.last_error">
          <span class="desc__label">最近错误</span>
          <span style="color: var(--c-red)">{{ task.last_error }}</span>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card__title">订单快照</div>
      <div class="desc">
        <div class="desc__item">
          <span class="desc__label">订单号</span>
          <router-link :to="{ name: 'order-detail', params: { id: task.order.id } }">
            {{ task.order.order_no }}
          </router-link>
        </div>
        <div class="desc__item"><span class="desc__label">客户</span>{{ task.order.customer_name }}</div>
        <div class="desc__item"><span class="desc__label">手机号</span>{{ task.order.phone }}</div>
        <div class="desc__item"><span class="desc__label">商品</span>{{ task.order.product_name }}</div>
        <div class="desc__item"><span class="desc__label">SKU</span>{{ task.order.sku }}</div>
        <div class="desc__item"><span class="desc__label">数量</span>{{ task.order.quantity }}</div>
        <div class="desc__item"><span class="desc__label">金额</span>{{ formatAmount(task.order.amount) }}</div>
        <div class="desc__item" style="grid-column: 1 / -1">
          <span class="desc__label">收货地址</span>{{ task.order.address }}
        </div>
        <div class="desc__item" style="grid-column: 1 / -1">
          <span class="desc__label">买家留言</span>{{ task.order.buyer_message || '-' }}
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card__title">依据的 AI 分析</div>
      <div v-if="task.analysis" class="desc">
        <div class="desc__item">
          <span class="desc__label">风险</span><RiskTag :value="task.analysis.risk_level" />
        </div>
        <div class="desc__item">
          <span class="desc__label">优先级</span><PriorityTag :value="task.analysis.priority" />
        </div>
        <div class="desc__item">
          <span class="desc__label">交期</span>{{ formatDeadline(task.analysis.deadline) }}
        </div>
        <div class="desc__item">
          <span class="desc__label">需联系客户</span>{{ formatBool(task.analysis.need_contact) }}
        </div>
        <div class="desc__item"><span class="desc__label">模型</span>{{ task.analysis.model_name || '-' }}</div>
        <div class="desc__item"><span class="desc__label">分析时间</span>{{ formatDateTime(task.analysis.created_at) }}</div>
        <div class="desc__item" style="grid-column: 1 / -1">
          <span class="desc__label">风险原因</span>{{ task.analysis.risk_reason || '-' }}
        </div>
        <div class="desc__item" style="grid-column: 1 / -1">
          <span class="desc__label">建议动作</span>{{ task.analysis.action || '-' }}
        </div>
      </div>
      <div v-else class="empty">该任务没有关联的 AI 分析</div>
    </div>

    <div class="card">
      <div class="card__title">执行记录</div>
      <div class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>尝试</th>
              <th>状态</th>
              <th>Worker</th>
              <th>开始</th>
              <th>结束</th>
              <th>耗时</th>
              <th>错误</th>
              <th>截图</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="ex in task.executions" :key="ex.id">
              <td>第 {{ ex.attempt }} 次</td>
              <td><StatusTag kind="execution" :value="ex.status" /></td>
              <td>{{ ex.worker_name || '-' }}</td>
              <td>{{ formatDateTime(ex.started_at) }}</td>
              <td>{{ formatDateTime(ex.finished_at) }}</td>
              <td>{{ formatDuration(ex.duration_ms) }}</td>
              <td style="color: var(--c-red)">{{ ex.error_message || '-' }}</td>
              <td>
                <button v-if="ex.screenshot_path" class="btn btn--sm" @click="viewScreenshot(ex)">
                  查看
                </button>
                <span v-else class="faint">-</span>
              </td>
            </tr>
            <tr v-if="task.executions.length === 0">
              <td colspan="8" class="empty">还没有执行记录</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </template>

  <ReviewDialog
    v-model="reviewVisible"
    :title="reviewDialogProps.title"
    :reason-label="reviewDialogProps.reasonLabel"
    :reason-required="reviewResult === 'REJECTED'"
    :reason-placeholder="reviewResult === 'REJECTED' ? '说明驳回理由（必填）' : '可补充审核意见（选填）'"
    :confirm-text="reviewDialogProps.confirmText"
    :confirm-danger="reviewDialogProps.danger"
    :loading="acting"
    @confirm="confirmReview"
  />

  <ReviewDialog
    v-model="retryVisible"
    title="重试任务"
    reason-label="处置说明"
    reason-placeholder="说明人工处置了什么（选填，仅记日志）"
    confirm-text="确认重试"
    show-checkbox
    checkbox-label="把重试次数归零（人工已解决根因，不再消耗自动重试次数）"
    :loading="acting"
    @confirm="confirmRetry"
  />

  <ReviewDialog
    v-model="cancelVisible"
    title="取消任务"
    reason-label="取消原因"
    reason-placeholder="说明为什么取消（选填）"
    confirm-text="确认取消"
    confirm-danger
    :loading="acting"
    @confirm="confirmCancel"
  />

  <div v-if="shotVisible" class="modal-mask" @click.self="closeScreenshot">
    <div class="modal" style="width: 720px">
      <div class="modal__head">失败截图</div>
      <div class="modal__body">
        <div v-if="shotLoading" class="empty">加载中…</div>
        <div v-else-if="shotError" class="empty" style="color: var(--c-red)">{{ shotError }}</div>
        <img v-else :src="shotUrl" class="thumb" alt="执行截图" />
      </div>
      <div class="modal__foot">
        <button class="btn" @click="closeScreenshot">关闭</button>
      </div>
    </div>
  </div>
</template>
