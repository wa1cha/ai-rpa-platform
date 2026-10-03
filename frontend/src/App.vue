<script setup>
// 应用根：只负责挂路由出口 + 全局 toast 容器。
// toast 内联在这里而不是单独一个组件，是为了不额外多出一个文件。
import { toastState, dismissToast } from '@/utils/toast'
</script>

<template>
  <router-view />

  <div class="toast-host">
    <transition-group name="toast">
      <div
        v-for="item in toastState.items"
        :key="item.id"
        class="toast"
        :class="`toast--${item.type}`"
        @click="dismissToast(item.id)"
      >
        {{ item.message }}
      </div>
    </transition-group>
  </div>
</template>
