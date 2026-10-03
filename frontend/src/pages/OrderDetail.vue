<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { getOrder, reanalyzeOrder } from '@/api/orders'
import PriorityTag from '@/components/PriorityTag.vue'
import ReviewDialog from '@/components/ReviewDialog.vue'
import RiskTag from '@/components/RiskTag.vue'
import StatusTag from '@/components/StatusTag.vue'
import {
  formatAmount,
  formatBool,
  formatDateTime,
  formatDeadline,
} from '@/utils/format'
import { toastSuccess } from '@/utils/toast'

const route = useRoute()
const router = useRouter()

const order = ref(null)
const loading = ref(true)

const reanalyzeVisible = ref(false)
const reanalyzing = ref(false)

// 只有「已分析 / 已建任务」才谈得上「重新」分析；
// 这里只是不给按钮，真正的状态校验在后端（409）。
const canReanalyze = computed(() =>
  ['ANALYZED', 'TASK_CREATED'].includes(order.value?.status),
)

async function load() {
  loading.value = true
  try {
    order.value = await getOrder(route.params.id)
  } finally {
    loading.value = false
  }
}

async function onReanalyze({ reason }) {
  reanalyzing.value = true
  try {
    await reanalyzeOrder(order.value.id, reason)
    reanalyzeVisible.value = false
    toastSuccess('已退回待分析，等待 AI 重新处理')
    await load()
  } finally {
    reanalyzing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page-head">
    <div class="row">
      <button class="btn btn--sm" @click="router.back()">← 返回</button>
      <div class="page-head__title">
        订单详情
        <span v-if="order" class="muted" style="font-size: 14px">#{{ order.order_no }}</span>
      </div>
    </div>
    <div class="row">
      <button
        v-if="order"
        class="btn"
        :disabled="!canReanalyze"
        :title="canReanalyze ? '' : '仅「已分析 / 已建任务」的订单可重分析'"
        @click="reanalyzeVisible = true"
      >
        重新分析
      </button>
      <StatusTag v-if="order" kind="order" :value="order.status" />
    </div>
  </div>

  <div v-if="loading" class="card empty">加载中…</div>

  <template v-else-if="order">
    <div class="card">
      <div class="card__title">基本信息</div>
      <div class="desc">
        <div class="desc__item"><span class="desc__label">平台</span>{{ order.platform }}</div>
        <div class="desc__item"><span class="desc__label">下单时间</span>{{ formatDateTime(order.ordered_at) }}</div>
        <div class="desc__item"><span class="desc__label">导入时间</span>{{ formatDateTime(order.imported_at) }}</div>
        <div class="desc__item"><span class="desc__label">客户</span>{{ order.customer_name }}</div>
        <div class="desc__item"><span class="desc__label">手机号</span>{{ order.phone }}</div>
        <div class="desc__item"><span class="desc__label">地址</span>{{ order.address }}</div>
        <div class="desc__item"><span class="desc__label">商品</span>{{ order.product_name }}</div>
        <div class="desc__item"><span class="desc__label">SKU</span>{{ order.sku }}</div>
        <div class="desc__item"><span class="desc__label">数量</span>{{ order.quantity }}</div>
        <div class="desc__item"><span class="desc__label">金额</span>{{ formatAmount(order.amount) }}</div>
        <div class="desc__item" style="grid-column: 1 / -1">
          <span class="desc__label">买家留言</span>{{ order.buyer_message || '-' }}
        </div>
        <div class="desc__item" style="grid-column: 1 / -1">
          <span class="desc__label">卖家备注</span>{{ order.seller_note || '-' }}
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card__title">最新 AI 分析</div>
      <div v-if="order.latest_analysis" class="desc">
        <div class="desc__item">
          <span class="desc__label">风险</span><RiskTag :value="order.latest_analysis.risk_level" />
        </div>
        <div class="desc__item">
          <span class="desc__label">优先级</span><PriorityTag :value="order.latest_analysis.priority" />
        </div>
        <div class="desc__item">
          <span class="desc__label">交期</span>{{ formatDeadline(order.latest_analysis.deadline) }}
        </div>
        <div class="desc__item">
          <span class="desc__label">需联系客户</span>{{ formatBool(order.latest_analysis.need_contact) }}
        </div>
        <div class="desc__item"><span class="desc__label">模型</span>{{ order.latest_analysis.model_name || '-' }}</div>
        <div class="desc__item"><span class="desc__label">分析时间</span>{{ formatDateTime(order.latest_analysis.created_at) }}</div>
        <div class="desc__item" style="grid-column: 1 / -1">
          <span class="desc__label">风险原因</span>{{ order.latest_analysis.risk_reason || '-' }}
        </div>
        <div class="desc__item" style="grid-column: 1 / -1">
          <span class="desc__label">建议动作</span>{{ order.latest_analysis.action || '-' }}
        </div>
      </div>
      <div v-else class="empty">该订单还没有 AI 分析结果</div>
    </div>

    <div class="card">
      <div class="card__title">任务</div>
      <div v-if="order.task" class="desc">
        <div class="desc__item">
          <span class="desc__label">任务号</span>
          <router-link :to="{ name: 'task-detail', params: { id: order.task.id } }">
            #{{ order.task.id }}
          </router-link>
        </div>
        <div class="desc__item">
          <span class="desc__label">状态</span><StatusTag kind="task" :value="order.task.status" />
        </div>
        <div class="desc__item">
          <span class="desc__label">优先级</span><PriorityTag :value="order.task.priority" />
        </div>
        <div class="desc__item">
          <span class="desc__label">重试</span>{{ order.task.retry_count }} / {{ order.task.max_retry }}
        </div>
        <div class="desc__item"><span class="desc__label">创建时间</span>{{ formatDateTime(order.task.created_at) }}</div>
      </div>
      <div v-else class="empty">该订单还没有生成任务</div>
    </div>

    <div class="card">
      <div class="card__title">人工修正记录</div>
      <div class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>字段</th>
              <th>原值</th>
              <th>新值</th>
              <th>修正人</th>
              <th>时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(log, index) in order.review_logs" :key="index">
              <td>{{ log.field_name }}</td>
              <td class="faint">{{ log.original_value ?? '-' }}</td>
              <td>{{ log.new_value ?? '-' }}</td>
              <td>{{ log.reviewer }}</td>
              <td>{{ formatDateTime(log.reviewed_at) }}</td>
            </tr>
            <tr v-if="order.review_logs.length === 0">
              <td colspan="5" class="empty">没有人工修正记录</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </template>

  <ReviewDialog
    v-model="reanalyzeVisible"
    title="重新触发 AI 分析"
    reason-label="重分析原因"
    reason-placeholder="说明为什么需要重新分析（选填，仅记日志）"
    confirm-text="确认重分析"
    :loading="reanalyzing"
    @confirm="onReanalyze"
  />
</template>
