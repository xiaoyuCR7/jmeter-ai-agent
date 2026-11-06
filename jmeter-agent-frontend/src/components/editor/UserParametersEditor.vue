<template>
  <div class="user-parameters-editor">
    <el-form-item :label="t('editor.userParams.title')">
      <div class="names-row">
        <el-input
          v-for="(_, idx) in paramNames"
          :key="'name-' + idx"
          :model-value="paramNames[idx]"
          @update:model-value="updateName(idx, $event)"
          size="small"
          :placeholder="t('editor.userParams.param') + (idx + 1)"
          style="width: 140px"
        />
        <el-button size="small" @click="addNameColumn">{{ t('editor.userParams.addColumn') }}</el-button>
        <el-button size="small" :disabled="paramNames.length <= 1" @click="removeNameColumn">{{ t('editor.userParams.removeColumn') }}</el-button>
      </div>
    </el-form-item>

    <el-form-item :label="t('editor.userParams.values')">
      <div class="table-scroll">
      <el-table :data="threadRows" border size="small" style="width: 100%">
        <el-table-column label="#" width="50" type="index" />
        <el-table-column
          v-for="(name, colIdx) in paramNames"
          :key="'col-' + colIdx"
          :label="name || t('editor.userParams.param') + (colIdx + 1)"
          min-width="140"
        >
          <template #default="{ $index }">
            <FunctionInput :model-value="threadRows[$index][colIdx]" @update:model-value="updateValue($index, colIdx, $event)" size="small" />
          </template>
        </el-table-column>
        <el-table-column width="80">
          <template #default="{ $index }">
            <el-button type="danger" size="small" text @click="removeRow($index)">{{ t('editor.common.delete') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
      </div>
      <el-button type="primary" text style="margin-top: 8px" @click="addRow">
        <el-icon><Plus /></el-icon> {{ t('editor.userParams.addRow') }}
      </el-button>
    </el-form-item>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from '@/composables/useI18n'
import FunctionInput from './FunctionInput.vue'

const { t } = useI18n()

const props = defineProps<{ modelValue: Record<string, any> }>()
const emit = defineEmits<{ 'update:modelValue': [value: Record<string, any>] }>()

const paramNames = computed({
  get(): string[] {
    const names = props.modelValue['UserParameters.names']
    if (Array.isArray(names) && names.length > 0) {
      return names.map((n: any) => (typeof n === 'string' ? n : String(n)))
    }
    return ['']
  },
  set(names: string[]) {
    syncToModel(names, threadRows.value)
  },
})

const threadRows = computed({
  get(): string[][] {
    const names = paramNames.value
    const threadValues = props.modelValue['UserParameters.thread_values']
    if (!Array.isArray(threadValues) || threadValues.length === 0) {
      return [Array(names.length).fill('')]
    }
    return threadValues.map((row: any) => {
      const arr = Array.isArray(row) ? row : []
      return names.map((_, i) => (typeof arr[i] === 'string' ? arr[i] : String(arr[i] ?? '')))
    })
  },
  set(rows: string[][]) {
    syncToModel(paramNames.value, rows)
  },
})

function syncToModel(names: string[], rows: string[][]) {
  emit('update:modelValue', {
    ...props.modelValue,
    'UserParameters.names': names,
    'UserParameters.thread_values': rows.map((row) =>
      names.map((_, i) => row[i] ?? '')
    ),
  })
}

function addNameColumn() {
  const names = [...paramNames.value, '']
  const rows = threadRows.value.map((row) => [...row, ''])
  syncToModel(names, rows)
}

function removeNameColumn() {
  if (paramNames.value.length <= 1) return
  const names = paramNames.value.slice(0, -1)
  const rows = threadRows.value.map((row) => row.slice(0, -1))
  syncToModel(names, rows)
}

function addRow() {
  syncToModel(paramNames.value, [...threadRows.value, Array(paramNames.value.length).fill('')])
}

function removeRow(index: number) {
  const rows = [...threadRows.value]
  rows.splice(index, 1)
  syncToModel(paramNames.value, rows.length ? rows : [Array(paramNames.value.length).fill('')])
}

// Each edit goes through syncToModel; editing the getters' copies never reached the plan.
function updateName(index: number, value: string) {
  syncToModel(paramNames.value.map((name, i) => i === index ? value : name), threadRows.value)
}

function updateValue(row: number, column: number, value: string) {
  syncToModel(paramNames.value, threadRows.value.map((values, i) =>
    i === row ? values.map((cell, j) => j === column ? value : cell) : values))
}
</script>

<style scoped lang="scss">
.names-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
</style>
