<script setup>
import { computed } from 'vue'

import { enumMeta } from '@/utils/format'

const props = defineProps({
  value: { type: String, default: '' },
})

const meta = computed(() => enumMeta('priority', props.value))
</script>

<template>
  <span class="tag" :class="`tag--${meta.tone}`">{{ meta.label }}</span>
</template>
