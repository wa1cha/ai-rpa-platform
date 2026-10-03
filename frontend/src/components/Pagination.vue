<script setup>
// 分页控件：受控组件，翻页只 emit('change', page)，数据由父级重新拉取。
// 分页是「服务端分页」（后端 Page 返回 items/total/page/page_size），
// 所以这里只负责渲染页码，不碰数据。
import { computed } from 'vue'

const props = defineProps({
  page: { type: Number, default: 1 },
  pageSize: { type: Number, default: 20 },
  total: { type: Number, default: 0 },
})

const emit = defineEmits(['change'])

const totalPages = computed(() =>
  Math.max(1, Math.ceil(props.total / props.pageSize)),
)

// 页码窗口：总页数不多时全列，多则「首页 … 当前±1 … 末页」。
const pages = computed(() => {
  const total = totalPages.value
  const cur = props.page
  if (total <= 7) {
    return Array.from({ length: total }, (_, i) => i + 1)
  }
  const out = [1]
  const start = Math.max(2, cur - 1)
  const end = Math.min(total - 1, cur + 1)
  if (start > 2) out.push('…')
  for (let i = start; i <= end; i += 1) out.push(i)
  if (end < total - 1) out.push('…')
  out.push(total)
  return out
})

function go(target) {
  if (typeof target !== 'number') return
  if (target < 1 || target > totalPages.value || target === props.page) return
  emit('change', target)
}
</script>

<template>
  <div class="pagination">
    <span>共 {{ total }} 条</span>
    <button class="page-btn" :disabled="page <= 1" @click="go(page - 1)">上一页</button>
    <div class="pagination__pages">
      <button
        v-for="(p, index) in pages"
        :key="index"
        class="page-btn"
        :class="{ 'page-btn--active': p === page }"
        :disabled="typeof p !== 'number'"
        @click="go(p)"
      >
        {{ p }}
      </button>
    </div>
    <button class="page-btn" :disabled="page >= totalPages" @click="go(page + 1)">
      下一页
    </button>
  </div>
</template>
