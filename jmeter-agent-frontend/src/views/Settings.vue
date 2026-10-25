<template>
  <div class="settings">
    <el-tabs v-model="activeTab" class="settings-tabs" @tab-change="onTabChange">
      <el-tab-pane :label="t('settings.aiModelConfig')" name="models">
        <el-card>
          <template #header>
            <div class="card-header">
              <span>{{ t('settings.aiModelConfig') }}</span>
              <el-button type="primary" size="small" @click="openAddDialog">
                <el-icon><Plus /></el-icon>{{ t('settings.addModel') }}
              </el-button>
            </div>
          </template>
          
          <div class="table-scroll">
            <el-table :data="customModelConfigs" style="width: 100%" v-loading="loading">
              <el-table-column prop="id" :label="t('settings.table.id')" min-width="100" />
            <el-table-column :label="t('settings.table.contextWindow')" width="120" align="right">
              <template #default="{ row }">
                <span v-if="row.context_window" class="num">{{ formatTokens(row.context_window) }}</span>
                <el-tooltip v-else :content="t('settings.form.contextWindowUnknown')">
                  <span class="muted">16K*</span>
                </el-tooltip>
              </template>
            </el-table-column>
            <el-table-column prop="enabled" :label="t('settings.table.status')" width="100">
              <template #default="{ row }">
                <el-tag size="small" :type="row.enabled ? 'success' : 'info'">
                  {{ row.enabled ? t('settings.table.enabled') : t('settings.table.disabled') }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="t('settings.table.actions')" min-width="200">
              <template #default="{ row }">
                <el-button link type="success" size="small" :loading="row.testing" @click="handleTest(row)">
                  {{ t('settings.table.test') }}
                </el-button>
                <el-button link type="primary" size="small" @click="openEditDialog(row)">
                  {{ t('settings.table.edit') }}
                </el-button>
                <el-button 
                  link 
                  type="danger" 
                  size="small" 
                  @click="handleDelete(row)"
                >
                  {{ t('settings.table.delete') }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
          </div>
        </el-card>
      </el-tab-pane>
      <el-tab-pane :label="t('layout.nav.mcp')" name="mcp" lazy>
        <MCPConnectPanel />
      </el-tab-pane>
      <el-tab-pane :label="t('settings.systemInfo')" name="system">
        <el-card>
          <template #header>
            <div class="card-header">
              <span>{{ t('settings.systemInfo') }}</span>
              <div v-if="editingPaths" class="header-actions">
                <el-button type="primary" size="small" @click="savePaths" :loading="savingPaths">
                  {{ t('settings.save') }}
                </el-button>
                <el-button size="small" @click="cancelEdit">{{ t('settings.cancel') }}</el-button>
              </div>
              <el-button v-else type="primary" size="small" @click="startEdit">
                {{ t('settings.editPaths') }}
              </el-button>
            </div>
          </template>
          <div v-if="systemInfo" class="info-list">
            <div class="info-item">
              <span class="label">{{ t('settings.info.platform') }}</span>
              <span class="value">{{ systemInfo.platform }}</span>
            </div>
            <div class="info-item">
              <span class="label">{{ t('settings.info.pythonVersion') }}</span>
              <span class="value">{{ systemInfo.python_version }}</span>
            </div>
            <div class="info-item">
              <span class="label">{{ t('settings.info.jmeterHome') }}</span>
              <span class="value">{{ systemInfo.jmeter_home }}</span>
            </div>
            <div class="info-item">
              <span class="label">{{ t('settings.info.jmeterExecutable') }}</span>
              <span class="value">{{ systemInfo.jmeter_executable || t('settings.info.notFound') }}</span>
            </div>
            <div class="info-item">
              <span class="label">{{ t('settings.info.testPlansDir') }}</span>
              <div v-if="editingPaths" class="value-edit">
                <el-input 
                  v-model="pathsForm.test_plans_dir" 
                  class="path-input"
                  :placeholder="t('settings.info.testPlansDirPlaceholder')"
                />
              </div>
              <span v-else class="value">{{ systemInfo.test_plans_dir }}</span>
            </div>
            <div class="info-item">
              <span class="label">{{ t('settings.info.reportsDir') }}</span>
              <div v-if="editingPaths" class="value-edit">
                <el-input 
                  v-model="pathsForm.reports_dir" 
                  class="path-input"
                  :placeholder="t('settings.info.reportsDirPlaceholder')"
                />
              </div>
              <span v-else class="value">{{ systemInfo.reports_dir }}</span>
            </div>
          </div>
        </el-card>
      </el-tab-pane>
      <el-tab-pane :label="t('settings.appearance')" name="appearance">
        <el-card>
          <template #header>
            <div class="card-header">
              <span>{{ t('settings.appearance') }}</span>
            </div>
          </template>
          <div class="theme-setting">
            <span class="theme-label">{{ t('settings.theme') }}</span>
            <el-radio-group v-model="appStore.currentTheme" size="small" @change="onThemeChange">
              <el-radio-button value="light">{{ t('settings.light') }}</el-radio-button>
              <el-radio-button value="dark">{{ t('settings.dark') }}</el-radio-button>
            </el-radio-group>
          </div>
          <p class="hint">{{ t('settings.themeHint') }}</p>

          <el-divider />

          <div class="theme-setting">
            <span class="theme-label">{{ t('settings.language') }}</span>
            <el-radio-group v-model="appStore.currentLang" size="small" @change="onLangChange">
              <el-radio-button value="zh-CN">{{ t('settings.language.chinese') }}</el-radio-button>
              <el-radio-button value="en">English</el-radio-button>
            </el-radio-group>
          </div>
          <p class="hint">{{ t('settings.languageHint') }}</p>
        </el-card>
      </el-tab-pane>
    </el-tabs>

    <!-- 添加/编辑模型对话框 -->
    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? t('settings.dialog.editTitle') : t('settings.dialog.addTitle')"
      :width="dialogWidth"
      destroy-on-close
    >
      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-width="90px"
        label-position="right"
      >
        <el-form-item :label="t('settings.form.provider')">
          <el-radio-group v-model="form.provider" @change="onProviderChange">
            <el-radio-button v-for="p in PROVIDERS" :key="p.key" :value="p.key">
              {{ p.label || t('settings.form.providerCustom') }}
            </el-radio-button>
          </el-radio-group>
        </el-form-item>

        <el-form-item :label="t('settings.form.model')" prop="model">
          <el-select
            v-model="form.model"
            filterable
            allow-create
            default-first-option
            :placeholder="t('settings.form.modelSelectPlaceholder')"
          >
            <el-option v-for="m in currentProvider.models" :key="m" :label="m" :value="m" />
          </el-select>
        </el-form-item>

        <el-form-item v-if="form.provider === 'custom'" :label="t('settings.form.apiBase')" prop="api_base">
          <el-input v-model="form.api_base" placeholder="https://api.xxx.com/v1" />
          <div class="form-tip">{{ t('settings.form.apiBaseCustomTip') }}</div>
        </el-form-item>

        <el-form-item :label="t('settings.form.apiKey')" prop="api_key">
          <el-input
            v-model="form.api_key"
            type="password"
            show-password
            :placeholder="isEdit ? t('settings.form.apiKeyKeep') : t('settings.form.apiKeyPlaceholder')"
          />
        </el-form-item>

        <el-form-item :label="t('settings.form.contextWindow')">
          <el-input-number v-model="form.context_window" :min="4096" :max="2000000" :step="1024"
            :controls="false" :value-on-clear="null" :placeholder="t('settings.form.contextWindowAuto')" class="full" />
          <div class="form-tip">{{ t('settings.form.contextWindowTip') }}</div>
        </el-form-item>

        <el-form-item v-if="isEdit" :label="t('settings.form.enabled')">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      
      <template #footer>
        <el-button @click="dialogVisible = false">{{ t('settings.cancel') }}</el-button>
        <el-button type="primary" @click="handleSubmit" :loading="submitting">
          {{ isEdit ? t('settings.save') : t('settings.add') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, reactive, computed, onUnmounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { api } from '@/api'
import { useAppStore } from '@/store'
import { useI18n } from '@/composables/useI18n'
import { useRoute, useRouter } from 'vue-router'
import MCPConnectPanel from '@/components/MCPConnectPanel.vue'

const { t, setLang } = useI18n()

const route = useRoute()
const router = useRouter()
const SETTINGS_TABS = ['models', 'mcp', 'system', 'appearance']
const activeTab = ref(SETTINGS_TABS.includes(route.query.tab as string) ? route.query.tab as string : 'models')
const onTabChange = (name: string | number) => router.replace({ query: name === 'models' ? {} : { tab: String(name) } })

const dialogWidth = ref('560px')

const handleResize = () => {
  if (window.innerWidth < 640) {
    dialogWidth.value = '95%'
  } else if (window.innerWidth < 768) {
    dialogWidth.value = '500px'
  } else {
    dialogWidth.value = '560px'
  }
}

const systemInfo = ref<any>(null)
const modelConfigs = ref<any[]>([])
const loading = ref(false)

const customModelConfigs = computed(() => {
  // Backend no longer returns built-in models; all returned configs are user-added.
  return modelConfigs.value
})
const dialogVisible = ref(false)
const isEdit = ref(false)
const submitting = ref(false)
const testing = ref(false)
const formRef = ref<any>(null)
const appStore = useAppStore()

const onThemeChange = (value: string) => {
  appStore.setTheme(value as 'light' | 'dark')
}

const onLangChange = (value: string) => {
  setLang(value as 'zh-CN' | 'en')
}

// 路径编辑相关
const editingPaths = ref(false)
const savingPaths = ref(false)
const pathsForm = reactive({
  test_plans_dir: '',
  reports_dir: ''
})

// 服务商预设：选中后自动带出 API 地址和适配器，无需手填。
// 模型名按各家 2026-10-25 的官方文档；列表外的名字也可以直接输入。
const PROVIDERS = [
  { key: 'deepseek', label: 'DeepSeek', api_base: 'https://api.deepseek.com/v1', adapter_type: 'openai_compatible',
    models: ['deepseek-flash', 'deepseek-v4-pro'] },
  { key: 'qwen', label: 'Qwen 通义千问', api_base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', adapter_type: 'openai_compatible',
    models: ['qwen-plus', 'qwen-flash', 'qwen-max'] },
  { key: 'openai', label: 'OpenAI', api_base: 'https://api.openai.com/v1', adapter_type: 'openai_compatible',
    models: ['gpt-6-astra', 'gpt-6.1-sol', 'gpt-6-luna'] },
  { key: 'anthropic', label: 'Claude', api_base: '', adapter_type: 'anthropic',
    models: ['claude-opus-5-5', 'claude-sonnet-5-5', 'claude-haiku-5-5', 'claude-fable-5-1'] },
  { key: 'custom', label: '', api_base: '', adapter_type: 'openai_compatible', models: [] as string[] }
]

const form = ref({
  id: '',
  provider: 'deepseek',
  model: '',
  api_key: '',
  api_base: '',
  context_window: null as number | null,
  enabled: true
})

const formatTokens = (n: number) => n >= 1_000_000 ? `${+(n / 1_000_000).toFixed(2)}M` : `${Math.round(n / 1024)}K`

const currentProvider = computed(() => PROVIDERS.find(p => p.key === form.value.provider) || PROVIDERS[PROVIDERS.length - 1])

const onProviderChange = () => {
  form.value.model = currentProvider.value.models[0] || ''
}

// 编辑时根据已保存的地址/适配器反推服务商
const detectProvider = (row: any) => {
  if (row.adapter_type === 'anthropic') return 'anthropic'
  // "https://api.deepseek.com" and ".../v1/" are the same endpoint.
  const base = (url: string) => (url || '').replace(/\/+$/, '').replace(/\/v1$/, '')
  return PROVIDERS.find(p => p.api_base && base(p.api_base) === base(row.api_base))?.key || 'custom'
}

// 由模型名生成唯一标识，如 deepseek-chat、deepseek-chat-2
const generateId = (model: string) => {
  const base = model.toLowerCase().replace(/[^a-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || 'model'
  const taken = new Set(modelConfigs.value.map((c: any) => c.id))
  let id = base
  for (let i = 2; taken.has(id); i++) id = `${base}-${i}`
  return id
}

const rules = computed(() => ({
  model: [{ required: true, message: t('settings.rule.modelIdRequired2'), trigger: 'blur' }],
  api_key: isEdit.value ? [] : [{ required: true, message: t('settings.rule.apiKeyRequired'), trigger: 'blur' }],
  api_base: [{ required: true, message: t('settings.rule.apiBaseRequired'), trigger: 'blur' }]
}))

const fetchData = async () => {
  loading.value = true
  try {
    const [sysRes, configsRes] = await Promise.all([
      api.getSystemInfo(),
      api.getModelConfigs()
    ])
    systemInfo.value = sysRes.data
    modelConfigs.value = configsRes.data
  } catch (error) {
    ElMessage.error(t('settings.message.loadFailed'))
  } finally {
    loading.value = false
  }
}

const startEdit = () => {
  if (systemInfo.value) {
    pathsForm.test_plans_dir = systemInfo.value.test_plans_dir
    pathsForm.reports_dir = systemInfo.value.reports_dir
    editingPaths.value = true
  }
}

const cancelEdit = () => {
  editingPaths.value = false
  pathsForm.test_plans_dir = ''
  pathsForm.reports_dir = ''
}

const savePaths = async () => {
  if (!pathsForm.test_plans_dir || !pathsForm.reports_dir) {
    ElMessage.error(t('settings.message.fillPaths'))
    return
  }
  
  savingPaths.value = true
  try {
    await api.updatePathsConfig({
      test_plans_dir: pathsForm.test_plans_dir,
      reports_dir: pathsForm.reports_dir
    })
    ElMessage.success(t('settings.message.pathsUpdated'))
    editingPaths.value = false
    pathsForm.test_plans_dir = ''
    pathsForm.reports_dir = ''
    await fetchData()
  } catch (error: any) {
    const msg = error.response?.data?.detail || t('settings.message.saveFailed')
    ElMessage.error(msg)
  } finally {
    savingPaths.value = false
  }
}

const openAddDialog = () => {
  isEdit.value = false
  form.value = {
    id: '',
    provider: PROVIDERS[0].key,
    model: PROVIDERS[0].models[0],
    api_key: '',
    api_base: '',
    context_window: null,
    enabled: true
  }
  dialogVisible.value = true
}

const openEditDialog = (row: any) => {
  isEdit.value = true
  form.value = {
    id: row.id,
    provider: detectProvider(row),
    model: row.model || '',
    api_key: '',
    api_base: row.api_base || '',
    context_window: row.context_window ?? null,
    enabled: row.enabled
  }
  dialogVisible.value = true
}

const handleSubmit = async () => {
  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) return
  
  submitting.value = true
  try {
    const provider = currentProvider.value
    const model = form.value.model.trim()
    const payload = {
      id: isEdit.value ? form.value.id : generateId(model),
      name: provider.label ? `${provider.label} ${model}` : model,
      model,
      // 编辑时留空：后端把 "***" 识别为保留原 Key
      api_key: form.value.api_key || '***',
      api_base: (provider.key === 'custom' ? form.value.api_base.trim() : provider.api_base) || null,
      adapter_type: provider.adapter_type,
      // Empty: the backend asks the provider for the real size.
      context_window: form.value.context_window || null,
      enabled: form.value.enabled
    }
    
    if (isEdit.value) {
      await api.updateModelConfig(form.value.id, payload)
      ElMessage.success(t('settings.message.updateSuccess'))
    } else {
      await api.addModelConfig(payload)
      ElMessage.success(t('settings.message.addSuccess'))
    }
    
    dialogVisible.value = false
    await fetchData()
  } catch (error: any) {
    const msg = error.response?.data?.detail || (isEdit.value ? t('settings.message.updateFailed') : t('settings.message.addFailed'))
    ElMessage.error(msg)
  } finally {
    submitting.value = false
  }
}

const handleTest = async (row: any) => {
  if (testing.value) return
  testing.value = true
  row.testing = true
  try {
    const res = await api.testModelConfig(row.id)
    if (res.data.success) {
      ElMessage.success(res.data.message || t('settings.message.testSuccess'))
    } else {
      ElMessage.error(res.data.message || t('settings.message.testFailed'))
    }
  } catch (error: any) {
    const msg = error.response?.data?.detail || t('settings.message.testFailed')
    ElMessage.error(msg)
  } finally {
    testing.value = false
    row.testing = false
  }
}

const handleDelete = async (row: any) => {
  try {
    await ElMessageBox.confirm(
      t('settings.confirmDeleteModel', { name: row.name }),
      t('settings.confirmDelete'),
      { type: 'warning', confirmButtonText: t('settings.confirmDelete'), cancelButtonText: t('settings.cancel') }
    )
    await api.deleteModelConfig(row.id)
    ElMessage.success(t('settings.message.deleteSuccess'))
    await fetchData()
  } catch (error: any) {
    if (error !== 'cancel') {
      const msg = error.response?.data?.detail || t('settings.message.deleteFailed')
      ElMessage.error(msg)
    }
  }
}

onMounted(() => {
  fetchData()
  window.addEventListener('resize', handleResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
})
</script>

<style scoped lang="scss">
.settings {
  max-width: 960px;
  margin: 0 auto;

  :deep(.el-dialog__body) {
    padding: 24px;

    @media (max-width: 768px) {
      padding: 16px;
    }
  }
  
  :deep(.el-form-item__label) {
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    flex-shrink: 0;
  }
  
  :deep(.el-form-item) {
    margin-bottom: 16px;
    display: flex;
    align-items: flex-start;
  }
  
  :deep(.el-form-item__content) {
    flex: 1;
    min-width: 0;
  }
  
  :deep(.el-input),
  :deep(.el-select) {
    width: 100%;
  }

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-weight: 500;
    color: var(--app-text);
    flex-wrap: wrap;
    gap: 12px;

    .el-button { width: auto; }

    .header-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
  }

  .info-list {
    .info-item {
      display: flex;
      justify-content: space-between;
      padding: 12px 0;
      border-bottom: 1px solid var(--app-border);
      align-items: flex-start;
      flex-wrap: wrap;
      gap: 8px;

      @media (max-width: 768px) {
        flex-direction: column;
        align-items: flex-start;

        .label {
          width: auto;
        }
      }

      &:last-child {
        border-bottom: none;
      }

      .label {
        color: var(--app-text-secondary);
        font-size: 14px;
        flex-shrink: 0;
        width: 100%;

        @media (min-width: 769px) {
          width: auto;
        }
      }

      .value {
        font-weight: 500;
        color: var(--app-text);
        max-width: 100%;
        text-align: left;
        overflow-wrap: break-word;
        word-break: normal;
        font-size: 14px;

        @media (min-width: 769px) {
          max-width: 60%;
          text-align: right;
        }
      }

      .value-edit {
        flex: 1;
        max-width: 100%;
        margin-left: 0;

        @media (min-width: 769px) {
          max-width: 60%;
          margin-left: auto;
        }

        .path-input {
          width: 100%;
        }
      }
    }
  }

  .hint {
    color: var(--app-text-secondary);
    font-size: 13px;
    line-height: 1.5;
  }

  .form-tip {
    font-size: 12px;
    color: var(--app-text-muted);
    line-height: 1.4;
    margin-top: 4px;
  }

  .theme-setting {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;
    flex-wrap: wrap;
    gap: 12px;

    .theme-label {
      color: var(--app-text-secondary);
      font-size: 14px;
    }
  }

  :deep(.el-table) {
    font-size: 13px;

    @media (max-width: 768px) {
      font-size: 12px;
    }
  }

  :deep(.el-table__header-wrapper),
  :deep(.el-table__body-wrapper) {
    overflow-x: auto;
  }

  .table-scroll {
    overflow-x: auto;
  }
}

// The dialog is teleported out of .settings, so its helpers sit at the top level.
.full { width: 100%; }
.muted { color: var(--app-text-muted); }
.num { font-variant-numeric: tabular-nums; }
</style>
