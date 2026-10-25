<template>
  <section class="panel">
    <ul class="progress">
      <li v-for="(step, index) in steps" :key="index" :class="{ done: step.done, active: step.active, error: step.error }">
        <el-icon v-if="step.error"><CircleCloseFilled /></el-icon>
        <el-icon v-else-if="step.done"><CircleCheckFilled /></el-icon>
        <el-icon v-else class="is-loading"><Loading /></el-icon>
        <span>{{ step.message }}</span>
      </li>
      <li v-if="!steps.length" class="active"><el-icon class="is-loading"><Loading /></el-icon>{{ L('正在连接…', 'Connecting…') }}</li>
    </ul>
    <div v-if="!running" class="actions">
      <el-button @click="emit('back')">{{ L('返回修改', 'Back') }}</el-button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { CircleCheckFilled, CircleCloseFilled, Loading } from '@element-plus/icons-vue'
import { useI18n } from '@/composables/useI18n'

defineProps<{ steps: { message: string; done: boolean; active: boolean; error: boolean }[]; running: boolean }>()
const emit = defineEmits<{ back: [] }>()
const { currentLang } = useI18n()
const L = (zh: string, en: string) => currentLang.value === 'en' ? en : zh
</script>

<style scoped lang="scss">
.progress {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  li { display: flex; align-items: center; gap: 10px; color: var(--app-text-muted); }
  li.active { color: var(--app-primary); }
  li.done { color: var(--app-text-secondary); .el-icon { color: var(--app-success); } }
  li.error { color: var(--app-danger); }
}
.actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }
</style>
