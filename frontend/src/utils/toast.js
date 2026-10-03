// 极简全局提示：一个响应式数组 + host 由 App.vue 渲染。
// 不引 UI 组件库是 Phase 6 的决定，所以「消息提示」这层也得自己来。
import { reactive } from 'vue'

export const toastState = reactive({ items: [] })

let seq = 0

export function toast(message, type = 'info', timeout = 3000) {
  const id = ++seq
  toastState.items.push({ id, message, type })
  if (timeout > 0) {
    setTimeout(() => dismissToast(id), timeout)
  }
  return id
}

export function dismissToast(id) {
  const index = toastState.items.findIndex((item) => item.id === id)
  if (index !== -1) {
    toastState.items.splice(index, 1)
  }
}

export const toastSuccess = (message) => toast(message, 'success')
export const toastError = (message) => toast(message, 'error')
export const toastInfo = (message) => toast(message, 'info')
