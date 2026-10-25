<template>
  <section class="panel">
    <el-segmented v-model="source" :options="sourceOptions" class="source-switch" />

    <template v-if="source === 'document'">
      <el-form label-position="top">
        <el-form-item :label="L('接口或需求文档', 'API or requirements document')" required>
          <div class="doc-input">
            <el-upload ref="uploadRef" :auto-upload="false" :limit="1" :show-file-list="false"
              accept=".json,.yaml,.yml,.txt,.md,.swagger" :on-change="handleFileChange">
              <el-button><el-icon><Upload /></el-icon> {{ L('读取文件', 'Load file') }}</el-button>
            </el-upload>
            <span v-if="attachedFile" class="file-chip">
              {{ attachedFile.name }} · {{ formatFileSize(attachedFile.size) }}
              <el-button link size="small" @click="clearAttachedFile"><el-icon><Close /></el-icon></el-button>
            </span>
            <span v-else class="hint">{{ L('支持 OpenAPI/Swagger（JSON、YAML）、Markdown、纯文本，10 MB 以内', 'OpenAPI/Swagger (JSON, YAML), Markdown or plain text, up to 10 MB') }}</span>
          </div>
          <el-input v-model="doc" type="textarea" :rows="10" resize="vertical"
            :placeholder="L('也可以直接粘贴文档内容', 'Or paste the document here')" />
        </el-form-item>
        <el-form-item :label="L('测试目标', 'What to test')" required>
          <el-input v-model="goal" type="textarea" :rows="4" resize="vertical"
            :placeholder="L('例如：测试登录和下单接口，500 并发持续 10 分钟，下单 P95 不超过 1 秒，错误率低于 0.1%', 'e.g. Test login and checkout at 500 concurrent users for 10 minutes; checkout P95 within 1 s, error rate below 0.1%')" />
          <div class="hint">{{ L('这里写的指标会和文档一起用来提取性能需求', 'Targets written here are used, with the document, to extract performance requirements') }}</div>
        </el-form-item>
        <el-form-item :label="L('模型', 'Model')">
          <el-select v-model="model" :placeholder="L('选择模型', 'Choose a model')" class="model-select">
            <el-option v-for="m in models" :key="m.id" :label="m.name" :value="m.id" />
          </el-select>
          <router-link v-if="!models.length" to="/settings" class="hint link">{{ L('还没有可用模型，去设置添加', 'No model yet: add one in Settings') }}</router-link>
        </el-form-item>
      </el-form>
      <div class="actions">
        <el-button type="primary" size="large" :loading="generating"
          :disabled="!doc.trim() || !goal.trim()" @click="emit('generate')">
          {{ L('开始生成', 'Generate') }}
        </el-button>
      </div>
    </template>

    <template v-else>
      <el-form label-position="top" @submit.prevent="emit('import')">
        <el-form-item :label="L('OpenAPI / Swagger 地址', 'OpenAPI / Swagger URL')" required>
          <el-input v-model="openApiUrl" placeholder="https://api.example.com/openapi.json" />
          <div class="hint">{{ L('按接口直接生成测试计划，不调用 AI；之后可在项目里补充性能需求', 'Builds test plans straight from the spec without AI; add performance requirements in the project afterwards') }}</div>
        </el-form-item>
      </el-form>
      <div class="actions">
        <el-button type="primary" size="large" :loading="importing" :disabled="!openApiUrl.trim()" @click="emit('import')">
          {{ L('导入', 'Import') }}
        </el-button>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { Upload, Close } from '@element-plus/icons-vue'
import type { UploadFile, UploadInstance } from 'element-plus'
import { api } from '@/api'
import { useI18n } from '@/composables/useI18n'

defineProps<{ generating: boolean; importing: boolean }>()
const emit = defineEmits<{ generate: []; import: [] }>()
const doc = defineModel<string>('doc', { required: true })
const goal = defineModel<string>('goal', { required: true })
const model = defineModel<string>('model', { required: true })
const openApiUrl = defineModel<string>('openApiUrl', { required: true })

const { t, currentLang } = useI18n()
const L = (zh: string, en: string) => currentLang.value === 'en' ? en : zh

const source = ref<'document' | 'openapi'>('document')
const sourceOptions = computed(() => [
  { label: L('上传或粘贴文档', 'Document'), value: 'document' },
  { label: L('OpenAPI 地址', 'OpenAPI URL'), value: 'openapi' },
])

const models = ref<any[]>([])
const uploadRef = ref<UploadInstance>()
const attachedFile = ref<{ name: string; size: number } | null>(null)

const handleFileChange = (file: UploadFile) => {
  const raw = file.raw
  if (!raw) return
  if (raw.size > 10 * 1024 * 1024) {
    ElMessage.warning(t('aiChat.fileSizeError'))
    return
  }
  const reader = new FileReader()
  reader.onload = (e) => {
    doc.value = e.target?.result as string
    attachedFile.value = { name: raw.name, size: raw.size }
  }
  reader.onerror = () => ElMessage.error(t('aiChat.fileReadError'))
  reader.readAsText(raw)
}

const clearAttachedFile = () => {
  attachedFile.value = null
  uploadRef.value?.clearFiles()
}

const formatFileSize = (bytes: number) => {
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / (1024 * 1024)).toFixed(2) + ' MB'
}

onMounted(async () => {
  try {
    models.value = (await api.getModels()).data
    if (models.value.length && !model.value) model.value = models.value[0].id
  } catch {
    models.value = []
  }
})
</script>

<style scoped lang="scss">
.source-switch { margin-bottom: 16px; }
.doc-input { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; width: 100%; margin-bottom: 8px; }
.file-chip { font-size: 13px; color: var(--app-text-secondary); background: var(--app-surface-muted); padding: 2px 4px 2px 10px; border-radius: 999px; }
.model-select { width: 240px; margin-right: 12px; }
.link { color: var(--app-primary); text-decoration: none; }
.actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }
</style>
