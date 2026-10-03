<script setup>
// 订单/任务状态标签。文案与颜色取自 utils/format.js，未知值原样灰色回显。
import { computed } from 'vue'

import { enumMeta } from '@/utils/format'

const props = defineProps({
  kind: { type: String, default: 'order' }, // 'order' | 'task'
  value: { type: String, default: '' },
})

const meta = computed(() => enumMeta(props.kind, props.value))
</script>

<template>
  <span class="tag" :class="`tag--${meta.tone}`">{{ meta.label }}</span>
</template>
