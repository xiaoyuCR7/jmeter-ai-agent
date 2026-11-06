<template>
  <div class="dns-hosts-editor">
    <el-form-item :label="t('editor.dnsHosts.title')">
      <div class="table-scroll">
      <el-table :data="hosts" border size="small" style="width: 100%">
        <el-table-column :label="t('editor.dnsHosts.hostname')" min-width="180">
          <template #default="{ $index }">
            <el-input :model-value="hosts[$index]['StaticHost.Name']" @update:model-value="updateCell($index, 'StaticHost.Name', $event)" size="small" />
          </template>
        </el-table-column>
        <el-table-column :label="t('editor.dnsHosts.ipAddress')" min-width="180">
          <template #default="{ $index }">
            <el-input :model-value="hosts[$index]['StaticHost.Address']" @update:model-value="updateCell($index, 'StaticHost.Address', $event)" size="small" />
          </template>
        </el-table-column>
        <el-table-column width="80">
          <template #default="{ $index }">
            <el-button type="danger" size="small" text @click="removeHost($index)">{{ t('editor.common.delete') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
      </div>
      <el-button type="primary" text style="margin-top: 8px" @click="addHost">
        <el-icon><Plus /></el-icon> {{ t('editor.dnsHosts.add') }}
      </el-button>
    </el-form-item>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useI18n } from '@/composables/useI18n'

const { t } = useI18n()

const props = defineProps<{ modelValue: Record<string, any> }>()
const emit = defineEmits<{ 'update:modelValue': [value: Record<string, any>] }>()

const hosts = computed({
  get() {
    const list = props.modelValue['DNSCacheManager.hosts']
    if (!Array.isArray(list)) return []
    return list.map((item: any) => ({
      _elementType: 'StaticHost',
      _name: item._name || '',
      'StaticHost.Name': item['StaticHost.Name'] || '',
      'StaticHost.Address': item['StaticHost.Address'] || '',
    }))
  },
  set(val) {
    emit('update:modelValue', { ...props.modelValue, 'DNSCacheManager.hosts': val })
  },
})

function addHost() {
  hosts.value = [
    ...hosts.value,
    { _elementType: 'StaticHost', _name: '', 'StaticHost.Name': '', 'StaticHost.Address': '' },
  ]
}

function removeHost(index: number) {
  const list = [...hosts.value]
  list.splice(index, 1)
  hosts.value = list
}

// A cell edit replaces the list, so the change reaches the plan (editing the getter's copy did not).
function updateCell(index: number, field: string, value: any) {
  hosts.value = hosts.value.map((item: any, i: number) => i === index ? { ...item, [field]: value } : item)
}
</script>
