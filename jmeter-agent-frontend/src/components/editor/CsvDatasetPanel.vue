<template>
  <div class="csv-dataset-config">
    <div class="config-header">
      <el-icon><Document /></el-icon> {{ t('editor.csvDataset') }}
    </div>
    <el-form label-position="top" size="small" class="csv-form">
      <el-form-item :label="t('editor.csv.filePath')">
        <el-input v-model="config.filePath" placeholder="/path/to/data.csv" size="small" />
        <div class="csv-hint">{{ t('editor.csv.fileHint') }}</div>
      </el-form-item>
      <el-form-item :label="t('editor.csv.variableNames')">
        <el-input v-model="config.variableNames" placeholder="username,password,email" size="small" />
      </el-form-item>
      <el-row :gutter="8">
        <el-col :span="12">
          <el-form-item :label="t('editor.csv.delimiter')">
            <el-input v-model="config.delimiter" placeholder="," size="small" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item :label="t('editor.csv.encoding')">
            <el-select v-model="config.encoding" size="small" style="width: 100%">
              <el-option label="UTF-8" value="UTF-8" />
              <el-option label="GBK" value="GBK" />
              <el-option label="ISO-8859-1" value="ISO-8859-1" />
            </el-select>
          </el-form-item>
        </el-col>
      </el-row>
      <el-row :gutter="8">
        <el-col :span="12">
          <el-form-item :label="t('editor.csv.ignoreFirstLine')">
            <el-switch v-model="config.ignoreFirstLine" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item :label="t('editor.csv.recycle')">
            <el-switch v-model="config.recycle" />
          </el-form-item>
        </el-col>
      </el-row>
      <el-form-item :label="t('editor.csv.preview')">
        <el-button text size="small" type="primary" @click="previewCSV" :disabled="!config.filePath">
          <el-icon><View /></el-icon> {{ t('editor.csv.preview') }}
        </el-button>
      </el-form-item>
    </el-form>
    <div v-if="csvPreviewData.length > 0" class="csv-preview">
      <el-table :data="csvPreviewData" size="small" border style="width: 100%">
        <el-table-column v-for="(col, idx) in csvPreviewColumns" :key="idx" :label="col" min-width="100" />
      </el-table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Document, View } from '@element-plus/icons-vue'
import { api } from '@/api'
import { useI18n } from '@/composables/useI18n'
import type { CsvDatasetConfig } from '@/composables/usePlanTree'

// The editor's own settings object: fields edited here are what it saves into the plan.
const props = defineProps<{ config: CsvDatasetConfig }>()
const csvDatasetConfig = props.config
const { t } = useI18n()

const csvPreviewData = ref<any[]>([])
const csvPreviewColumns = ref<string[]>([])

async function previewCSV() {
  try {
    const res = await api.previewCSV(csvDatasetConfig.filePath, 5)
    if (res.data) {
      csvPreviewColumns.value = res.data.columns
      csvPreviewData.value = res.data.rows.map((row: string[]) =>
        Object.fromEntries(res.data.columns.map((column: string, index: number) => [column, row[index] ?? ''])))
    }
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || t('editor.csv.previewError'))
  }
}
</script>

<style scoped lang="scss">
.csv-dataset-config {
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

  .csv-form {
    .el-form-item {
      margin-bottom: 8px;
    }

    .el-form-item__label {
      font-size: 12px;
      padding-bottom: 4px;
    }
  }

  .csv-hint {
    font-size: 11px;
    color: var(--app-text-secondary);
    margin-top: 4px;
  }
}

.csv-preview {
  margin-top: 12px;
  :deep(.el-table) {
    font-size: 12px;
  }
}
</style>
