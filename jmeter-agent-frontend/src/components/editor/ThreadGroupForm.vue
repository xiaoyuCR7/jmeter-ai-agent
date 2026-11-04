<template>
  <div class="thread-group-config">
    <div class="config-header">
      <el-icon><User /></el-icon> {{ t('editor.threadGroup') }}
    </div>
    <p v-if="node" class="hint">
      {{ L(`仅配置首个线程组：${node.testname}。复杂配置请切换到 JMX 树视图。`,
           `Only the first thread group, ${node.testname}, is edited here. Use the JMX tree for anything more.`) }}
    </p>
    <el-form label-position="top" size="small" class="tg-form">
      <el-row :gutter="8">
        <el-col v-for="field in [FIELDS.numThreads, FIELDS.rampTime, FIELDS.loops]" :key="field.key" :span="12">
          <el-form-item :label="t(field.label)">
            <el-input-number v-if="editable(field)" v-model="config[field.key]" :min="field.min" :max="field.max" style="width: 100%" />
            <template v-else>
              <el-input :model-value="String(numberValue(field.property, field.fallback))" readonly />
              <span class="hint">{{ KEPT() }}</span>
            </template>
            <span v-if="field.unit" class="unit">{{ field.unit() }}</span>
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item :label="t('editor.tg.onSampleError')">
            <el-select v-model="config.onSampleError" size="small" style="width: 100%">
              <el-option :label="t('editor.tg.errorContinue')" value="continue" />
              <el-option :label="t('editor.tg.errorStartNextLoop')" value="startnextloop" />
              <el-option :label="t('editor.tg.errorStopThread')" value="stopthread" />
              <el-option :label="t('editor.tg.errorStopTest')" value="stoptest" />
              <el-option :label="t('editor.tg.errorStopTestNow')" value="stoptestnow" />
            </el-select>
          </el-form-item>
        </el-col>
      </el-row>
      <el-form-item>
        <el-switch v-model="config.scheduler" @change="schedulerChanged" :label="t('editor.tg.scheduler')" />
      </el-form-item>
      <el-row v-if="config.scheduler" :gutter="8">
        <el-col v-for="field in [FIELDS.duration, FIELDS.delay]" :key="field.key" :span="12">
          <el-form-item :label="t(field.label)">
            <el-input-number v-if="editable(field)" v-model="config[field.key]" :min="field.min" :max="field.max" style="width: 100%" />
            <template v-else>
              <el-input :model-value="String(numberValue(field.property, field.fallback))" readonly />
              <span class="hint">{{ KEPT() }}</span>
            </template>
            <span class="unit">{{ field.unit?.() }}</span>
          </el-form-item>
        </el-col>
      </el-row>
    </el-form>
  </div>
</template>

<script setup lang="ts">
import { User } from '@element-plus/icons-vue'
import { useI18n } from '@/composables/useI18n'
import type { ThreadGroupConfig } from '@/composables/usePlanTree'

type NumberKey = 'numThreads' | 'rampTime' | 'loops' | 'duration' | 'delay'
interface NumberField {
  key: NumberKey; property: string; fallback: number; min: number; max: number; label: string; unit?: () => string
}

// The editor's own settings object: fields edited here are what it saves into the plan. A value the form cannot
// show losslessly (a variable, an expression) is displayed read-only, so saving never rewrites it.
const props = defineProps<{
  config: ThreadGroupConfig
  node: any
  numberValue: (property: string, fallback: number) => any
  canEdit: (value: any, min: number, max: number) => boolean
}>()
const { t, L } = useI18n()

const seconds = () => L('秒', 's')
const KEPT = () => L('保留原始值；请切换到 JMX 树视图修改。', 'Kept as written; change it in the JMX tree.')
const FIELDS: Record<NumberKey, NumberField> = {
  numThreads: { key: 'numThreads', property: 'ThreadGroup.num_threads', fallback: 1, min: 1, max: 10000, label: 'editor.tg.numThreads' },
  rampTime: { key: 'rampTime', property: 'ThreadGroup.ramp_time', fallback: 1, min: 0, max: 3600, label: 'editor.tg.rampTime', unit: seconds },
  loops: { key: 'loops', property: 'loops', fallback: 1, min: -1, max: 99999, label: 'editor.tg.loops', unit: () => t('editor.tg.infinite') },
  duration: { key: 'duration', property: 'ThreadGroup.duration', fallback: 0, min: 1, max: 86400, label: 'editor.tg.duration', unit: seconds },
  delay: { key: 'delay', property: 'ThreadGroup.delay', fallback: 0, min: 0, max: 86400, label: 'editor.tg.delay', unit: seconds },
}

const editable = (field: NumberField) => props.canEdit(props.numberValue(field.property, field.fallback), field.min, field.max)

function schedulerChanged(enabled: string | number | boolean) {
  // Only a user's switch action initializes an unset duration; mount remains lossless.
  if (enabled === true && props.canEdit(props.numberValue('ThreadGroup.duration', 0), 0, 0)) {
    props.config.duration = 1
  }
}
</script>

<style scoped lang="scss">
.thread-group-config {
  padding: 12px;
  border-bottom: 1px solid var(--app-border);

  .config-header {
    font-size: 13px;
    font-weight: 500;
    color: var(--app-text);
    margin-bottom: 10px;
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .tg-form {
    :deep(.el-form-item) { margin-bottom: 8px; }
    :deep(.el-form-item__label) { font-size: 12px; padding-bottom: 4px; }
  }

  .hint {
    font-size: 11px;
    color: var(--app-text-secondary);
    margin-top: 4px;
  }
}
</style>
