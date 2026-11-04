<template>
  <span v-if="!verdict || verdict.verdict === 'none'" class="verdict muted">{{ L('未设需求', 'No requirements') }}</span>
  <span v-else :class="['verdict', verdict.verdict]" :title="detail">
    <span class="dot"></span>{{ label }}
  </span>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from '@/composables/useI18n'

export interface VerdictSummary {
  verdict: 'pass' | 'fail' | 'incomplete' | 'none'
  passed: number
  failed: number
  missing: number
}

const props = defineProps<{ verdict?: VerdictSummary | null }>()
const { L } = useI18n()

const label = computed(() => {
  const v = props.verdict!
  if (v.verdict === 'pass') return L(`达标 ${v.passed}/${v.passed}`, `Pass ${v.passed}/${v.passed}`)
  if (v.verdict === 'fail') return L(`不达标 ${v.failed}`, `Fail ${v.failed}`)
  return L('数据不全', 'Incomplete')
})
const detail = computed(() => {
  const v = props.verdict!
  return L(`达标 ${v.passed} · 不达标 ${v.failed} · 无数据 ${v.missing}`, `${v.passed} pass · ${v.failed} fail · ${v.missing} no data`)
})
</script>

<style scoped lang="scss">
.verdict {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;

  .dot { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
  &.pass { color: var(--app-success); background: var(--app-success-soft); }
  &.fail { color: var(--app-danger); background: var(--app-danger-soft); }
  &.incomplete { color: var(--app-warning); background: var(--app-warning-soft); }
  &.muted { color: var(--app-text-muted); padding: 0; font-weight: 400; }
}
</style>
