<template>
  <div class="auth-editor">
    <el-form-item :label="t('editor.auth.title')">
      <div class="table-scroll">
      <el-table :data="entries" border size="small" style="width: 100%">
        <el-table-column label="URL" min-width="160">
          <template #default="{ $index }">
            <FunctionInput :model-value="entries[$index]['Authorization.url']" @update:model-value="updateCell($index, 'Authorization.url', $event)" size="small" />
          </template>
        </el-table-column>
        <el-table-column :label="t('editor.auth.username')" min-width="100">
          <template #default="{ $index }">
            <FunctionInput :model-value="entries[$index]['Authorization.username']" @update:model-value="updateCell($index, 'Authorization.username', $event)" size="small" />
          </template>
        </el-table-column>
        <el-table-column :label="t('editor.auth.password')" min-width="100">
          <template #default="{ $index }">
            <FunctionInput :model-value="entries[$index]['Authorization.password']" @update:model-value="updateCell($index, 'Authorization.password', $event)" size="small" />
          </template>
        </el-table-column>
        <el-table-column :label="t('editor.auth.domain')" min-width="90">
          <template #default="{ $index }">
            <el-input :model-value="entries[$index]['Authorization.domain']" @update:model-value="updateCell($index, 'Authorization.domain', $event)" size="small" />
          </template>
        </el-table-column>
        <el-table-column label="Realm" min-width="90">
          <template #default="{ $index }">
            <el-input :model-value="entries[$index]['Authorization.realm']" @update:model-value="updateCell($index, 'Authorization.realm', $event)" size="small" />
          </template>
        </el-table-column>
        <el-table-column width="70">
          <template #default="{ $index }">
            <el-button type="danger" size="small" text @click="removeEntry($index)">{{ t('editor.common.delete') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
      </div>
      <el-button type="primary" text style="margin-top: 8px" @click="addEntry">
        <el-icon><Plus /></el-icon> {{ t('editor.auth.add') }}
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

const entries = computed({
  get() {
    const list = props.modelValue['AuthManager.auth_list']
    if (!Array.isArray(list)) return []
    return list.map((item: any) => ({
      _elementType: 'Authorization',
      _name: item._name || '',
      'Authorization.url': item['Authorization.url'] || '',
      'Authorization.username': item['Authorization.username'] || '',
      'Authorization.password': item['Authorization.password'] || '',
      'Authorization.domain': item['Authorization.domain'] || '',
      'Authorization.realm': item['Authorization.realm'] || '',
    }))
  },
  set(val) {
    emit('update:modelValue', { ...props.modelValue, 'AuthManager.auth_list': val })
  },
})

function addEntry() {
  entries.value = [
    ...entries.value,
    {
      _elementType: 'Authorization',
      _name: '',
      'Authorization.url': '',
      'Authorization.username': '',
      'Authorization.password': '',
      'Authorization.domain': '',
      'Authorization.realm': '',
    },
  ]
}

function removeEntry(index: number) {
  const list = [...entries.value]
  list.splice(index, 1)
  entries.value = list
}

// A cell edit replaces the list, so the change reaches the plan (editing the getter's copy did not).
function updateCell(index: number, field: string, value: any) {
  entries.value = entries.value.map((item: any, i: number) => i === index ? { ...item, [field]: value } : item)
}
</script>
