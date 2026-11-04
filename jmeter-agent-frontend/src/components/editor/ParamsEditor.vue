<template>
  <div>
    <el-table :data="params" border size="small">
      <el-table-column v-for="field in FIELDS" :key="field.key" :label="t(field.label)" :min-width="field.width">
        <template #default="{ $index }">
          <el-input v-model="params[$index][field.key]" :placeholder="t(field.label)" size="small" />
        </template>
      </el-table-column>
      <el-table-column width="60">
        <template #default="{ $index }">
          <el-button text size="small" type="danger" @click="params.splice($index, 1)">
            <el-icon><Delete /></el-icon>
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-button text type="primary" size="small" @click="params.push({ name: '', value: '', description: '' })" style="margin-top: 6px">
      <el-icon><Plus /></el-icon> {{ t('editor.addParam') }}
    </el-button>
  </div>
</template>

<script setup lang="ts">
import { Delete, Plus } from '@element-plus/icons-vue'
import { useI18n } from '@/composables/useI18n'
import type { EndpointData } from '@/composables/usePlanTree'

// The endpoint's own query parameter list, edited in place: it is what the editor saves.
defineProps<{ params: EndpointData['queryParams'] }>()
const { t } = useI18n()

const FIELDS = [
  { key: 'name', label: 'editor.paramName', width: 160 },
  { key: 'value', label: 'editor.paramValue', width: 200 },
  { key: 'description', label: 'editor.paramDescription', width: 120 },
] as const
</script>
