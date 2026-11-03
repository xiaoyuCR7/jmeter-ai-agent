<template>
  <div class="project-settings">
    <el-tabs v-model="activeSettingsTab" tab-position="left" class="settings-tabs">
      <!-- Basic Info Tab -->
      <el-tab-pane :label="t('projectSettings.tab.basic')" name="info">
        <el-card shadow="never" class="info-card">
          <el-form :model="projectForm" label-position="top" style="max-width: 560px">
            <el-form-item :label="t('projectSettings.form.name')" required>
              <el-input v-model="projectForm.name" :placeholder="t('projectSettings.form.namePlaceholder')" />
            </el-form-item>
            <el-form-item :label="t('projectSettings.form.description')">
              <el-input v-model="projectForm.description" type="textarea" :rows="3" :placeholder="t('projectSettings.form.descriptionPlaceholder')" />
            </el-form-item>
            <el-form-item>
              <el-button type="primary" @click="saveProjectInfo" :loading="savingInfo">
                {{ t('projectSettings.form.save') }}
              </el-button>
            </el-form-item>
          </el-form>
        </el-card>
        <el-card shadow="never" class="info-card danger-zone">
          <div class="danger-row">
            <div>
              <div class="danger-title">{{ localText('删除项目', 'Delete project') }}</div>
              <div class="danger-hint">{{ localText('同时删除项目下的测试计划、执行记录和报告、需求、基线、监控配置，以及它积累的经验和假设，无法恢复。', 'Also deletes its test plans, runs and reports, requirements, baselines, monitoring settings, and the lessons and hypotheses it gathered. This cannot be undone.') }}</div>
            </div>
            <el-button type="danger" plain @click="deleteProject">{{ localText('删除项目', 'Delete project') }}</el-button>
          </div>
        </el-card>
      </el-tab-pane>

      <!-- Environments Tab -->
      <el-tab-pane :label="t('projectSettings.tab.environments')" name="environments">
        <div class="env-header">
          <div class="env-header-left">
            <span class="env-count">{{ t('projectSettings.env.count', { count: environments.length }) }}</span>
          </div>
          <el-button type="primary" size="small" @click="showAddEnvDialog">
            <el-icon><Plus /></el-icon> {{ t('projectSettings.env.add') }}
          </el-button>
        </div>

        <div v-if="environments.length === 0" class="env-empty">
          <el-empty :description="t('projectSettings.env.empty')" :image-size="60">
            <el-button type="primary" @click="showAddEnvDialog">{{ t('projectSettings.env.createFirst') }}</el-button>
          </el-empty>
        </div>

        <div v-else class="env-cards">
          <el-card
            v-for="env in environments"
            :key="env.id"
            :class="['env-card', { active: activeEnvId === env.id }]"
            shadow="never"
          >
            <template #header>
              <div class="env-card-header">
                <div class="env-card-title">
                  <el-icon v-if="activeEnvId === env.id" color="var(--app-primary)" size="16">
                    <CircleCheckFilled />
                  </el-icon>
                  <span>{{ env.name }}</span>
                  <el-tag v-if="activeEnvId === env.id" size="small" type="success">{{ t('projectSettings.env.active') }}</el-tag>
                  <el-tag v-if="env.kind === 'production'" size="small" type="danger">{{ kindLabel('production') }}</el-tag>
                </div>
                <div class="env-card-actions">
                  <el-button
                    v-if="activeEnvId !== env.id"
                    size="small"
                    text
                    type="primary"
                   
                    @click="setActive(env.id)"
                  >
                    {{ t('projectSettings.env.setActive') }}
                  </el-button>
                  <el-button size="small" text @click="editEnv(env)">
                    <el-icon><Edit /></el-icon>
                  </el-button>
                  <el-button size="small" text @click="duplicateEnv(env)">
                    <el-icon><CopyDocument /></el-icon>
                  </el-button>
                  <el-button size="small" text type="danger" @click="deleteEnv(env)" :disabled="environments.length <= 1">
                    <el-icon><Delete /></el-icon>
                  </el-button>
                </div>
              </div>
            </template>

            <el-form label-position="top" size="small" class="env-form">
              <el-form-item label="Base URL" class="wide">
                <el-input v-model="env.base_url" placeholder="https://api.example.com" @change="updateEnv(env)" />
              </el-form-item>
              <el-form-item :label="localText('环境类型', 'Kind')">
                <el-select v-model="env.kind" @change="updateEnv(env)">
                  <el-option v-for="kind in envKinds" :key="kind.value" :label="kind.label" :value="kind.value" />
                </el-select>
              </el-form-item>
              <el-form-item :label="localText('最大线程数', 'Thread cap')">
                <el-input-number v-model="env.max_threads" :min="1" :max="100000" :step="10" controls-position="right"
                  :placeholder="localText('不限制', 'No cap')" @change="updateEnv(env)" />
              </el-form-item>
              <el-form-item :label="localText('Prometheus 地址（可选）', 'Prometheus URL (optional)')" class="wide">
                <el-input v-model="env.prometheus_url" clearable placeholder="http://prometheus:9090"
                  @change="updateEnv(env)" />
              </el-form-item>
            </el-form>
            <p class="env-hint">
              {{ env.kind === 'production'
                ? localText('生产环境：每次运行前都会请你确认。', 'Production: every run asks you first.')
                : '' }}
              {{ localText('运行时请求发往这里的地址；下面的请求头和变量会覆盖计划里同名的。不填 Prometheus 地址时用项目的监控配置。',
                           'Runs send requests to this address; the headers and variables below replace ones of the same name in the plan. Without a Prometheus URL the project monitoring is used.') }}
            </p>

            <el-divider>{{ t('projectSettings.env.variables') }}</el-divider>
            <div class="env-table-wrapper">
              <el-table :data="env.varRows || []" border size="small">
                <el-table-column :label="t('projectSettings.env.varName')" min-width="150">
                  <template #default="{ row }">
                    <el-input v-model="row.key" size="small" :placeholder="t('projectSettings.env.varNamePlaceholder')" @change="applyEnvVars(env)" />
                  </template>
                </el-table-column>
                <el-table-column :label="t('projectSettings.env.varValue')" min-width="200">
                  <template #default="{ row }">
                    <el-input v-model="row.value" size="small" :type="row.secret ? 'password' : 'text'"
                      :placeholder="t('projectSettings.env.varValuePlaceholder')" @change="applyEnvVars(env)" />
                  </template>
                </el-table-column>
                <el-table-column :label="localText('敏感', 'Secret')" width="64" align="center">
                  <template #default="{ row }">
                    <el-checkbox v-model="row.secret" :aria-label="localText('敏感值', 'Secret value')" @change="applyEnvVars(env)" />
                  </template>
                </el-table-column>
                <el-table-column width="60">
                  <template #default="{ $index }">
                    <el-button text size="small" type="danger" @click="removeEnvVar(env, $index)">
                      <el-icon><Delete /></el-icon>
                    </el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            <el-button text size="small" type="primary" @click="addEnvVar(env)" style="margin-top: 6px">
              <el-icon><Plus /></el-icon> {{ t('projectSettings.env.addVar') }}
            </el-button>

            <el-divider>{{ t('projectSettings.env.headers') }}</el-divider>
            <div class="env-table-wrapper">
              <el-table :data="env.headerRows || []" border size="small">
                <el-table-column :label="t('projectSettings.env.headerName')" min-width="150">
                  <template #default="{ row }">
                    <el-input v-model="row.key" size="small" :placeholder="t('projectSettings.env.headerNamePlaceholder')" @change="applyEnvHeaders(env)" />
                  </template>
                </el-table-column>
                <el-table-column :label="t('projectSettings.env.headerValue')" min-width="200">
                  <template #default="{ row }">
                    <el-input v-model="row.value" size="small" :type="row.secret ? 'password' : 'text'"
                      :placeholder="t('projectSettings.env.headerValuePlaceholder')" @change="applyEnvHeaders(env)" />
                  </template>
                </el-table-column>
                <el-table-column :label="localText('敏感', 'Secret')" width="64" align="center">
                  <template #default="{ row }">
                    <el-checkbox v-model="row.secret" :aria-label="localText('敏感值', 'Secret value')" @change="applyEnvHeaders(env)" />
                  </template>
                </el-table-column>
                <el-table-column width="60">
                  <template #default="{ $index }">
                    <el-button text size="small" type="danger" @click="removeEnvHeader(env, $index)">
                      <el-icon><Delete /></el-icon>
                    </el-button>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            <el-button text size="small" type="primary" @click="addEnvHeader(env)" style="margin-top: 6px">
              <el-icon><Plus /></el-icon> {{ t('projectSettings.env.addHeader') }}
            </el-button>
          </el-card>
        </div>
      </el-tab-pane>

      <el-tab-pane :label="localText('服务端监控', 'Server monitoring')" name="monitoring" lazy>
        <MonitoringPanel :project-id="projectId" />
      </el-tab-pane>
    </el-tabs>

    <!-- Add/Edit Environment Dialog -->
    <el-dialog
      v-model="envDialogVisible"
      :title="editingEnvId ? t('projectSettings.env.edit') : t('projectSettings.env.add')"
      width="480px"
      style="max-width: calc(100vw - 32px);"
      destroy-on-close
    >
      <el-form :model="envDialogForm" label-position="top">
        <el-form-item :label="t('projectSettings.env.name')" required>
          <el-input v-model="envDialogForm.name" :placeholder="t('projectSettings.env.namePlaceholder')" />
        </el-form-item>
        <el-form-item label="Base URL">
          <el-input v-model="envDialogForm.base_url" placeholder="https://api.example.com" />
        </el-form-item>
        <el-form-item :label="localText('环境类型', 'Kind')">
          <el-select v-model="envDialogForm.kind" style="width: 100%">
            <el-option v-for="kind in envKinds" :key="kind.value" :label="kind.label" :value="kind.value" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="envDialogVisible = false">{{ t('projectSettings.cancel') }}</el-button>
        <el-button type="primary" @click="submitEnvDialog" :loading="savingEnv">
          {{ editingEnvId ? t('projectSettings.save') : t('projectSettings.create') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, watch, computed } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  Plus, Edit, CopyDocument, Delete, CircleCheckFilled
} from '@element-plus/icons-vue'
import { api } from '@/api'
import MonitoringPanel from '@/components/MonitoringPanel.vue'
import { useI18n } from '@/composables/useI18n'

const { t, currentLang } = useI18n()
const localText = (zh: string, en: string) => currentLang.value === 'en' ? en : zh

interface KVRow { key: string; value: string; secret?: boolean }

type EnvKind = 'development' | 'testing' | 'staging' | 'production'

interface EnvData {
  id: string
  name: string
  base_url: string
  variables: Record<string, string>
  headers: Record<string, string>
  kind: EnvKind
  secret_keys: string[]
  max_threads: number | null
  prometheus_url: string | null
  varRows?: KVRow[]
  headerRows?: KVRow[]
}

const KIND_LABELS: Record<EnvKind, [string, string]> = {
  development: ['开发', 'Development'], testing: ['测试', 'Testing'],
  staging: ['预发', 'Staging'], production: ['生产', 'Production'],
}
const kindLabel = (kind: EnvKind) => localText(...KIND_LABELS[kind])
const envKinds = computed(() => (Object.keys(KIND_LABELS) as EnvKind[]).map(value => ({ value, label: kindLabel(value) })))

// Secret values arrive as ****** and go back unchanged unless edited, which the server reads as "keep it".
const toRows = (values: Record<string, string>, secret: string[]): KVRow[] =>
  Object.entries(values || {}).map(([key, value]) => ({ key, value: String(value), secret: secret.includes(key) }))
const secretKeys = (env: EnvData) => [...(env.varRows || []), ...(env.headerRows || [])]
  .filter(row => row.secret && row.key.trim()).map(row => row.key.trim())

const props = defineProps<{ projectId: string }>()
const emit = defineEmits<{ updated: []; deleted: [] }>()
const route = useRoute()
const projectId = computed(() => props.projectId)

const SECTIONS = ['info', 'environments', 'monitoring']
const activeSettingsTab = ref(SECTIONS.includes(route.query.section as string) ? route.query.section as string : 'info')
watch(() => route.query.section, section => {
  if (SECTIONS.includes(section as string)) activeSettingsTab.value = section as string
})
const savingInfo = ref(false)
const savingEnv = ref(false)
// Project form
const projectForm = reactive({
  name: '',
  description: '',
})

// Environments
const environments = ref<EnvData[]>([])
const activeEnvId = ref<string | null>(null)

// Env dialog
const envDialogVisible = ref(false)
const editingEnvId = ref<string | null>(null)
const envDialogForm = reactive({
  name: '',
  base_url: '',
  kind: 'testing' as EnvKind,
})

// Rebuild a record from table rows; returns null when duplicate keys exist.
function rowsToRecord(rows: KVRow[]): Record<string, string> | null {
  const out = new Map<string, string>()
  for (const r of rows) {
    const k = r.key.trim()
    if (!k) continue
    if (out.has(k)) return null
    out.set(k, r.value)
  }
  return Object.fromEntries(out)
}

function applyEnvVars(env: EnvData) {
  const rec = rowsToRecord(env.varRows || [])
  if (rec === null) {
    ElMessage.error(localText('变量名不能重复', 'Variable names must be unique'))
    return
  }
  env.variables = rec
  updateEnv(env)
}

function applyEnvHeaders(env: EnvData) {
  const rec = rowsToRecord(env.headerRows || [])
  if (rec === null) {
    ElMessage.error(localText('请求头名称不能重复', 'Header names must be unique'))
    return
  }
  env.headers = rec
  updateEnv(env)
}

function addEnvVar(env: EnvData) {
  if (!env.varRows) env.varRows = []
  env.varRows.push({ key: '', value: '' })
}

function removeEnvVar(env: EnvData, index: number) {
  env.varRows?.splice(index, 1)
  applyEnvVars(env)
}

function addEnvHeader(env: EnvData) {
  if (!env.headerRows) env.headerRows = []
  env.headerRows.push({ key: '', value: '' })
}

function removeEnvHeader(env: EnvData, index: number) {
  env.headerRows?.splice(index, 1)
  applyEnvHeaders(env)
}

// Load data
async function loadSettings() {
  projectForm.name = ''
  projectForm.description = ''
  environments.value = []
  activeEnvId.value = null
  envDialogVisible.value = false
  await Promise.all([loadProject(), loadEnvironments()])
}
watch(projectId, loadSettings, { immediate: true })

async function loadProject() {
  const id = projectId.value
  try {
    const res = await api.getProject(id)
    if (id !== projectId.value) return
    projectForm.name = res.data.name || ''
    projectForm.description = res.data.description || ''
  } catch (e: any) {
    ElMessage.error(t('projectSettings.message.loadFailed'))
  }
}

async function loadEnvironments() {
  const id = projectId.value
  try {
    const res = await api.getProjectEnvironments(id)
    if (id !== projectId.value) return
    if (res.data?.environments) {
      environments.value = res.data.environments.map((e: any) => ({
        ...e,
        variables: e.variables || {},
        headers: e.headers || {},
        kind: e.kind || 'testing',
        secret_keys: e.secret_keys || [],
        max_threads: e.max_threads ?? null,
        prometheus_url: e.prometheus_url ?? null,
        varRows: toRows(e.variables, e.secret_keys || []),
        headerRows: toRows(e.headers, e.secret_keys || []),
      }))
    }
    activeEnvId.value = res.data?.active_id || null
  } catch {
    if (id === projectId.value) environments.value = []
  }
}

// Actions
async function saveProjectInfo() {
  savingInfo.value = true
  try {
    await api.updateProject(projectId.value, {
      name: projectForm.name,
      description: projectForm.description || null,
    })
    ElMessage.success(t('projectSettings.message.saveSuccess'))
    emit('updated')
  } catch (e: any) {
    ElMessage.error(t('projectSettings.message.saveFailed') + ': ' + (e.response?.data?.detail || e.message))
  } finally {
    savingInfo.value = false
  }
}

async function deleteProject() {
  const id = projectId.value
  try {
    await ElMessageBox.confirm(
      t('testPlans.confirmDeleteProject', { name: projectForm.name }),
      localText('删除项目', 'Delete project'),
      { type: 'warning', confirmButtonText: t('projectSettings.delete'), cancelButtonText: t('projectSettings.cancel') }
    )
  } catch { return }
  try {
    await api.deleteProject(id)
    ElMessage.success(t('testPlans.message.projectDeleted'))
    emit('deleted')
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || t('projectSettings.message.deleteFailed'))
  }
}

function showAddEnvDialog() {
  editingEnvId.value = null
  envDialogForm.name = ''
  envDialogForm.base_url = ''
  envDialogForm.kind = 'testing'
  envDialogVisible.value = true
}

function editEnv(env: EnvData) {
  editingEnvId.value = env.id
  envDialogForm.name = env.name
  envDialogForm.base_url = env.base_url
  envDialogForm.kind = env.kind
  envDialogVisible.value = true
}

async function submitEnvDialog() {
  if (!envDialogForm.name.trim()) {
    ElMessage.warning(t('projectSettings.env.nameRequired'))
    return
  }

  savingEnv.value = true
  try {
    if (editingEnvId.value) {
      await api.updateEnvironment(projectId.value, editingEnvId.value, {
        name: envDialogForm.name,
        base_url: envDialogForm.base_url,
        kind: envDialogForm.kind,
      })
      ElMessage.success(t('projectSettings.message.envUpdated'))
    } else {
      await api.createEnvironment(projectId.value, {
        name: envDialogForm.name,
        base_url: envDialogForm.base_url,
        kind: envDialogForm.kind,
        variables: {},
        headers: {},
      })
      ElMessage.success(t('projectSettings.message.envCreated'))
    }
    envDialogVisible.value = false
    await loadEnvironments()
  } catch (e: any) {
    ElMessage.error(t('projectSettings.message.operationFailed') + ': ' + (e.response?.data?.detail || e.message))
  } finally {
    savingEnv.value = false
  }
}

// Auto-saves send the whole record, so they must reach the server in edit order.
const envSaves = new Map<string, Promise<void>>()

function updateEnv(env: EnvData) {
  const id = projectId.value
  env.secret_keys = secretKeys(env)
  const payload = {
    base_url: env.base_url,
    variables: { ...env.variables },
    headers: { ...env.headers },
    secret_keys: env.secret_keys,
    kind: env.kind,
    max_threads: env.max_threads || null,
    clear_max_threads: !env.max_threads,
    prometheus_url: env.prometheus_url || null,
  }
  const save = (envSaves.get(env.id) || Promise.resolve()).then(async () => {
    try {
      await api.updateEnvironment(id, env.id, payload)
    } catch (e: any) {
      ElMessage.error(t('projectSettings.message.saveFailed') + ': ' + (e.response?.data?.detail || e.message))
    }
  })
  envSaves.set(env.id, save)
  return save
}

async function setActive(envId: string) {
  try {
    await api.setActiveEnvironment(projectId.value, envId)
    activeEnvId.value = envId
    ElMessage.success(t('projectSettings.message.envSwitched'))
  } catch (e: any) {
    ElMessage.error(t('projectSettings.message.switchFailed'))
  }
}

async function duplicateEnv(env: EnvData) {
  try {
    await api.createEnvironment(projectId.value, {
      name: env.name + t('projectSettings.env.copySuffix'),
      base_url: env.base_url,
      variables: env.variables,
      headers: env.headers,
      kind: env.kind,
      secret_keys: env.secret_keys,
      max_threads: env.max_threads,
      prometheus_url: env.prometheus_url,
      copy_from: env.id,
    })
    ElMessage.success(t('projectSettings.message.envCopied'))
    await loadEnvironments()
  } catch (e: any) {
    ElMessage.error(t('projectSettings.message.copyFailed'))
  }
}

async function deleteEnv(env: EnvData) {
  const id = projectId.value
  if (environments.value.length <= 1) {
    ElMessage.warning(t('projectSettings.env.atLeastOne'))
    return
  }
  try {
    await ElMessageBox.confirm(
      t('projectSettings.env.confirmDelete', { name: env.name }),
      t('projectSettings.confirmDelete'),
      { type: 'warning', confirmButtonText: t('projectSettings.delete'), cancelButtonText: t('projectSettings.cancel') }
    )
    if (id !== projectId.value) return
    await api.deleteEnvironment(id, env.id)
    ElMessage.success(t('projectSettings.message.envDeleted'))
    await loadEnvironments()
  } catch (e: any) {
    if (e !== 'cancel') {
      ElMessage.error(t('projectSettings.message.deleteFailed'))
    }
  }
}
</script>

<style scoped lang="scss">
.project-settings {
  .settings-tabs :deep(.el-tabs__content) { padding-left: 8px; }
  @media (max-width: 768px) {
    .settings-tabs { flex-direction: column; }
  }

  .info-card { margin-bottom: 16px; }
  .danger-zone { border-color: var(--app-danger-soft); }
  .danger-row { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; }
  .danger-title { font-weight: 500; color: var(--app-text); }
  .danger-hint { font-size: 12px; color: var(--app-text-muted); }

  .env-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
    .env-count { color: var(--app-text-secondary); }
  }

  .env-empty { padding: 40px 0; }
  .env-cards { display: flex; flex-direction: column; gap: 16px; }

  .env-card {
    &.active { border-color: var(--app-primary); }
    .env-card-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
    }
    .env-card-title {
      display: flex;
      align-items: center;
      gap: 8px;
      font-weight: 500;
      color: var(--app-text);
      min-width: 0;
      span { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    }
    .env-card-actions { display: flex; gap: 4px; }
  }

  .env-table-wrapper { overflow-x: auto; }
  .env-form {
    display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); column-gap: 12px;
    .wide { grid-column: 1 / -1; }
    .el-select, .el-input-number { width: 100%; }
    @media (max-width: 560px) { grid-template-columns: 1fr; }
  }
  .env-hint { margin: 0 0 4px; font-size: 12px; line-height: 1.6; color: var(--app-text-muted); }
}
</style>
