<template>
  <div class="tab-content">
    <el-form label-position="top" size="small">
      <el-row :gutter="16">
        <el-col :span="8">
          <el-form-item :label="t('editor.protocol')">
            <el-select v-model="endpoint.protocol" style="width: 100%">
              <el-option label="" value="" />
              <el-option label="http" value="http" />
              <el-option label="https" value="https" />
            </el-select>
          </el-form-item>
        </el-col>
        <el-col :span="8">
          <el-form-item :label="t('editor.server')">
            <el-input v-model="endpoint.domain" placeholder="api.example.com" />
          </el-form-item>
        </el-col>
        <el-col :span="8">
          <el-form-item :label="t('editor.port')">
            <el-input v-model="endpoint.port" placeholder="443" />
          </el-form-item>
        </el-col>
      </el-row>
      <el-row :gutter="16">
        <el-col :span="8">
          <el-form-item :label="t('editor.connectionTimeout')">
            <el-input-number v-if="canEdit(endpoint._rawTimeout ?? endpoint.settings.timeout, 0, Number.MAX_SAFE_INTEGER)"
              v-model="endpoint.settings.timeout" :min="0" style="width: 100%" />
            <template v-else>
              <el-input :model-value="String(endpoint._rawTimeout)" readonly />
              <span class="hint">{{ L('保留原始值；请切换到 JMX 树视图修改。', 'Kept as written; change it in the JMX tree.') }}</span>
            </template>
          </el-form-item>
        </el-col>
        <el-col :span="8">
          <el-form-item :label="t('editor.contentEncoding')">
            <el-input v-model="endpoint.settings.encoding" placeholder="UTF-8" />
          </el-form-item>
        </el-col>
      </el-row>
      <el-row :gutter="16">
        <el-col :span="8">
          <el-form-item :label="t('editor.followRedirects')">
            <el-switch v-model="endpoint.settings.followRedirects" />
          </el-form-item>
        </el-col>
        <el-col :span="8">
          <el-form-item :label="t('editor.autoRedirect')">
            <el-switch v-model="endpoint.settings.autoRedirects" />
          </el-form-item>
        </el-col>
        <el-col :span="8">
          <el-form-item label="KeepAlive">
            <el-switch v-model="endpoint.settings.keepAlive" />
          </el-form-item>
        </el-col>
      </el-row>
    </el-form>
  </div>
</template>

<script setup lang="ts">
import { useI18n } from '@/composables/useI18n'
import type { EndpointData } from '@/composables/usePlanTree'

// The selected endpoint itself, read through the prop as the selection changes: what is edited here is saved.
defineProps<{ endpoint: EndpointData; canEdit: (value: any, min: number, max: number) => boolean }>()
const { t, L } = useI18n()
</script>

<style scoped lang="scss">
.hint { font-size: 11px; color: var(--app-text-secondary); margin-top: 4px; }
</style>
