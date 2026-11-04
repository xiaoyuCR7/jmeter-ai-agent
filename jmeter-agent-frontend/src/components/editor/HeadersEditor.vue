<template>
  <div class="headers-editor">
    <el-form-item :label="t('editor.headers.title')">
      <div class="table-scroll">
      <el-table :data="headers" border size="small" style="width: 100%">
        <el-table-column :label="t('editor.headers.name')" min-width="180">
          <template #default="{ $index }">
            <FunctionInput :model-value="headers[$index]['Header.name']" @update:model-value="updateHeader($index, 'Header.name', $event)" size="small" />
          </template>
        </el-table-column>
        <el-table-column :label="t('editor.headers.value')" min-width="240">
          <template #default="{ $index }">
            <FunctionInput :model-value="headers[$index]['Header.value']" @update:model-value="updateHeader($index, 'Header.value', $event)" size="small" />
          </template>
        </el-table-column>
        <el-table-column width="80">
          <template #default="{ $index }">
            <el-button type="danger" size="small" text @click="removeHeader($index)">
              {{ t('editor.common.delete') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      </div>
      <el-button type="primary" text style="margin-top: 8px" @click="addHeader">
        <el-icon><Plus /></el-icon> {{ t('editor.headers.add') }}
      </el-button>
    </el-form-item>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { Plus } from '@element-plus/icons-vue'
import { useI18n } from '@/composables/useI18n'
import FunctionInput from './FunctionInput.vue'

const { t } = useI18n()

const props = defineProps<{
  modelValue: Record<string, any>
}>()

const emit = defineEmits<{
  'update:modelValue': [value: Record<string, any>]
}>()

const headers = computed({
  get() {
    const list = props.modelValue['HeaderManager.headers']
    if (!Array.isArray(list)) return []
    return list.map((item: any) => ({
      ...item,
      '_elementType': item._elementType || 'Header',
      '_name': item._name || '',
      'Header.name': item['Header.name'] || '',
      'Header.value': item['Header.value'] || '',
    }))
  },
  set(val) {
    emit('update:modelValue', {
      ...props.modelValue,
      'HeaderManager.headers': val,
    })
  },
})

function updateHeader(index: number, field: 'Header.name' | 'Header.value', value: string) {
  headers.value = headers.value.map((header: any, i: number) => i === index ? { ...header, [field]: value } : header)
}

function addHeader() {
  const list = [...headers.value]
  list.push({
    _elementType: 'Header',
    _name: '',
    'Header.name': '',
    'Header.value': '',
  })
  headers.value = list
}

function removeHeader(index: number) {
  const list = [...headers.value]
  list.splice(index, 1)
  headers.value = list
}
</script>
