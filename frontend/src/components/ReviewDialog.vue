<script setup>
// 通用确认弹窗：一个可选/必填的「原因」输入（+ 可选的单个复选框）+ 确认/取消。
// 任务审核、任务取消/重试、订单重分析、AI 修正共用它，靠 props 调文案、必填与否。
//
// confirm 事件统一回传 { reason, checked } 一个对象：调用方解构自己关心的字段，
// 不必因为「有的场景多一个复选框」就分裂出第二种回调签名。
import { ref, watch } from 'vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  title: { type: String, default: '确认操作' },
  reasonLabel: { type: String, default: '原因' },
  reasonPlaceholder: { type: String, default: '可填写原因（选填）' },
  reasonRequired: { type: Boolean, default: false },
  reasonMaxLength: { type: Number, default: 500 },
  confirmText: { type: String, default: '确认' },
  confirmDanger: { type: Boolean, default: false },
  loading: { type: Boolean, default: false },
  showCheckbox: { type: Boolean, default: false },
  checkboxLabel: { type: String, default: '' },
  checkboxDefault: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue', 'confirm'])

const reason = ref('')
const checked = ref(false)

// 每次打开都重置，避免上次的输入被误提交。
watch(
  () => props.modelValue,
  (visible) => {
    if (visible) {
      reason.value = ''
      checked.value = props.checkboxDefault
    }
  },
)

function close() {
  emit('update:modelValue', false)
}

function onConfirm() {
  const value = reason.value.trim()
  if (props.reasonRequired && !value) return
  emit('confirm', { reason: value, checked: checked.value })
}
</script>

<template>
  <div v-if="modelValue" class="modal-mask" @click.self="close">
    <div class="modal">
      <div class="modal__head">{{ title }}</div>
      <div class="modal__body">
        <div class="field" style="margin-bottom: 0">
          <label class="field__label">
            {{ reasonLabel }}
            <span v-if="reasonRequired" style="color: var(--c-red)">*</span>
          </label>
          <textarea
            v-model="reason"
            class="textarea"
            rows="4"
            :maxlength="reasonMaxLength"
            :placeholder="reasonPlaceholder"
          ></textarea>
          <div class="faint" style="margin-top: 4px; text-align: right; font-size: 12px">
            {{ reason.length }} / {{ reasonMaxLength }}
          </div>
        </div>

        <label v-if="showCheckbox" class="chip" style="margin-top: 10px">
          <input v-model="checked" type="checkbox" />
          {{ checkboxLabel }}
        </label>
      </div>
      <div class="modal__foot">
        <button class="btn" :disabled="loading" @click="close">取消</button>
        <button
          class="btn"
          :class="confirmDanger ? 'btn--danger' : 'btn--primary'"
          :disabled="loading || (reasonRequired && !reason.trim())"
          @click="onConfirm"
        >
          {{ loading ? '处理中…' : confirmText }}
        </button>
      </div>
    </div>
  </div>
</template>
