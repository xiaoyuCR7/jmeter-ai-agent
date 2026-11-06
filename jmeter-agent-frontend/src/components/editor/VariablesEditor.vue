<template>
  <div class="variables-editor">
    <el-form-item :label="t('editor.variables.title')">
      <div class="variables-toolbar">
        <el-button type="primary" text size="small" @click="addVariable">
          <el-icon><Plus /></el-icon> {{ t('editor.variables.add') }}
        </el-button>
        <el-button text size="small" @click="exportCSV">
          <el-icon><Download /></el-icon> {{ t('editor.variables.export') }}
        </el-button>
        <el-button text size="small" @click="triggerImport">
          <el-icon><Upload /></el-icon> {{ t('editor.variables.import') }}
        </el-button>
        <input ref="fileInput" type="file" accept=".csv" style="display: none" @change="handleImport" />
      </div>
      <div class="table-scroll">
      <el-table :data="variables" border size="small" style="width: 100%">
        <el-table-column :label="t('editor.variables.name')" min-width="180">
          <template #default="{ $index }">
            <el-input :model-value="variables[$index]['Argument.name']" @update:model-value="updateCell($index, 'Argument.name', $event)" size="small" />
          </template>
        </el-table-column>
        <el-table-column :label="t('editor.variables.value')" min-width="240">
          <template #default="{ $index }">
            <FunctionInput :model-value="variables[$index]['Argument.value']" @update:model-value="updateCell($index, 'Argument.value', $event)" size="small" />
          </template>
        </el-table-column>
        <el-table-column :label="t('editor.variables.desc')" min-width="150">
          <template #default="{ $index }">
            <el-input :model-value="variables[$index]['Argument.desc']" @update:model-value="updateCell($index, 'Argument.desc', $event)" size="small" />
          </template>
        </el-table-column>
        <el-table-column width="80">
          <template #default="{ $index }">
            <el-button type="danger" size="small" text @click="removeVariable($index)">
              {{ t('editor.common.delete') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
      </div>
    </el-form-item>
  </div>
</template>

<style scoped lang="scss">
.variables-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 8px;
}
</style>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Plus, Download, Upload } from '@element-plus/icons-vue'
import { useI18n } from '@/composables/useI18n'
import FunctionInput from './FunctionInput.vue'

const { t } = useI18n()

const props = defineProps<{
  modelValue: Record<string, any>
}>()

const emit = defineEmits<{
  'update:modelValue': [value: Record<string, any>]
}>()

const fileInput = ref<HTMLInputElement | null>(null)

const variables = computed({
  get() {
    const list = props.modelValue['Arguments.arguments']
    if (!Array.isArray(list)) return []
    return list.map((item: any) => ({
      '_elementType': item._elementType || 'Argument',
      '_name': item._name || '',
      'Argument.name': item['Argument.name'] || '',
      'Argument.value': item['Argument.value'] || '',
      'Argument.desc': item['Argument.desc'] || item['Argument.metadata'] || '',
    }))
  },
  set(val) {
    emit('update:modelValue', {
      ...props.modelValue,
      'Arguments.arguments': val,
    })
  },
})

function addVariable() {
  const list = [...variables.value]
  list.push({
    _elementType: 'Argument',
    _name: '',
    'Argument.name': '',
    'Argument.value': '',
    'Argument.desc': '',
  })
  variables.value = list
}

function removeVariable(index: number) {
  const list = [...variables.value]
  list.splice(index, 1)
  variables.value = list
}

function exportCSV() {
  const header = ['name', 'value', 'description']
  const rows = variables.value.map(v => [
    v['Argument.name'] || '',
    v['Argument.value'] || '',
    v['Argument.desc'] || '',
  ])
  const csv = [header.join(','), ...rows.map(r => r.map(c => `"${c.replace(/"/g, '""')}"`).join(','))].join('\n')
  
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = 'variables.csv'
  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  URL.revokeObjectURL(url)
  ElMessage.success(t('editor.variables.exportSuccess'))
}

function triggerImport() {
  fileInput.value?.click()
}

function handleImport(event: Event) {
  const target = event.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file) return

  const reader = new FileReader()
  reader.onload = (e) => {
    const content = e.target?.result as string
    try {
      const rows = content.split('\n').filter(r => r.trim())
      const importList: any[] = []
      
      for (let i = 1; i < rows.length; i++) {
        const line = rows[i]
        const match = line.match(/^"([^"]*)","([^"]*)","([^"]*)"$/)
        if (match) {
          importList.push({
            _elementType: 'Argument',
            _name: match[1],
            'Argument.name': match[1],
            'Argument.value': match[2],
            'Argument.desc': match[3],
          })
        }
      }

      if (importList.length > 0) {
        variables.value = [...variables.value, ...importList]
        ElMessage.success(t('editor.variables.importSuccess', { count: importList.length }))
      } else {
        ElMessage.warning(t('editor.variables.importEmpty'))
      }
    } catch {
      ElMessage.error(t('editor.variables.importError'))
    }
  }
  reader.readAsText(file)
  target.value = ''
}

// A cell edit replaces the list, so the change reaches the plan (editing the getter's copy did not).
function updateCell(index: number, field: string, value: any) {
  variables.value = variables.value.map((item: any, i: number) => i === index ? { ...item, [field]: value } : item)
}
</script>
