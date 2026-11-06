<template>
  <div class="cookies-editor">
    <el-form-item :label="t('editor.cookies.title')">
      <div class="table-scroll">
      <el-table :data="cookies" border size="small" style="width: 100%">
        <el-table-column :label="t('editor.cookies.name')" min-width="120">
          <template #default="{ $index }">
            <el-input :model-value="cookies[$index]['Cookie.name']" @update:model-value="updateCell($index, 'Cookie.name', $event)" size="small" />
          </template>
        </el-table-column>
        <el-table-column :label="t('editor.cookies.value')" min-width="140">
          <template #default="{ $index }">
            <FunctionInput :model-value="cookies[$index]['Cookie.value']" @update:model-value="updateCell($index, 'Cookie.value', $event)" size="small" />
          </template>
        </el-table-column>
        <el-table-column :label="t('editor.cookies.domain')" min-width="120">
          <template #default="{ $index }">
            <el-input :model-value="cookies[$index]['Cookie.domain']" @update:model-value="updateCell($index, 'Cookie.domain', $event)" size="small" />
          </template>
        </el-table-column>
        <el-table-column :label="t('editor.cookies.path')" width="100">
          <template #default="{ $index }">
            <el-input :model-value="cookies[$index]['Cookie.path']" @update:model-value="updateCell($index, 'Cookie.path', $event)" size="small" />
          </template>
        </el-table-column>
        <el-table-column width="70">
          <template #default="{ $index }">
            <el-button type="danger" size="small" text @click="removeCookie($index)">{{ t('editor.common.delete') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
      </div>
      <el-button type="primary" text style="margin-top: 8px" @click="addCookie">
        <el-icon><Plus /></el-icon> {{ t('editor.cookies.add') }}
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

const props = defineProps<{ modelValue: Record<string, any> }>()
const emit = defineEmits<{ 'update:modelValue': [value: Record<string, any>] }>()

const cookies = computed({
  get() {
    const list = props.modelValue['CookieManager.cookies']
    if (!Array.isArray(list)) return []
    return list.map((item: any) => ({
      _elementType: 'Cookie',
      _name: item._name || item['Cookie.name'] || '',
      // JMeter names a cookie by its element name; there is no Cookie.name property.
      'Cookie.name': item['Cookie.name'] ?? item._name ?? '',
      'Cookie.value': item['Cookie.value'] || '',
      'Cookie.domain': item['Cookie.domain'] || '',
      'Cookie.path': item['Cookie.path'] || '/',
      'Cookie.secure': item['Cookie.secure'] || false,
      'Cookie.expires': item['Cookie.expires'] || '0',
    }))
  },
  set(val) {
    const written = val.map(({ 'Cookie.name': name, ...cookie }: any) => ({ ...cookie, _name: name || '' }))
    emit('update:modelValue', { ...props.modelValue, 'CookieManager.cookies': written })
  },
})

function addCookie() {
  cookies.value = [
    ...cookies.value,
    {
      _elementType: 'Cookie',
      _name: '',
      'Cookie.name': '',
      'Cookie.value': '',
      'Cookie.domain': '',
      'Cookie.path': '/',
      'Cookie.secure': false,
      'Cookie.expires': '0',
    },
  ]
}

function removeCookie(index: number) {
  const list = [...cookies.value]
  list.splice(index, 1)
  cookies.value = list
}

// A cell edit replaces the list, so the change reaches the plan (editing the getter's copy did not).
function updateCell(index: number, field: string, value: any) {
  cookies.value = cookies.value.map((item: any, i: number) => i === index ? { ...item, [field]: value } : item)
}
</script>
