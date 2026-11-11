<template>
  <div class="test-case-editor">
    <!-- Toolbar -->
    <div class="editor-toolbar">
      <div class="toolbar-left">
        <el-button @click="goBack" text :title="projectName">
          <el-icon><ArrowLeft /></el-icon> {{ t('editor.back') }}
        </el-button>
        <el-divider direction="vertical" />
        <span class="plan-name" :title="planName">{{ planName }}</span>
        <el-segmented :model-value="viewMode" :options="viewModeOptions" size="small" class="view-switch"
          :disabled="switchingView" @change="switchView" />
      </div>
      <div v-if="viewMode === 'form'" class="toolbar-center">
        <EnvironmentSelector
          :environments="envList"
          :active-env-id="activeEnvId"
          :active-env="activeEnv"
          @update:active-env-id="handleEnvChange"
          @manage="goToProjectSettings"
          @copy-env="handleCopyEnv"
        />
      </div>
      <div v-if="viewMode === 'form'" class="toolbar-right">
        <el-button @click="handleSave" :loading="saving">
          <el-icon><DocumentChecked /></el-icon> {{ t('editor.save') }}
        </el-button>
        <el-button type="primary" @click="handleRun" :loading="running">
          <el-icon><VideoPlay /></el-icon> {{ t('editor.run') }}
        </el-button>
        <el-button @click="handleViewSource">
          <el-icon><Document /></el-icon> {{ t('editor.viewJmx') }}
        </el-button>
        <el-button @click="functionHelperVisible = true">
          <span class="fx-label">fx</span> {{ t('editor.functionHelper') }}
        </el-button>
      </div>
    </div>

    <!-- Body: 3-panel layout -->
    <div v-if="viewMode === 'form'" class="editor-body">
      <!-- Left: Thread Group Config + Endpoint List -->
      <div class="left-panel">
        <ThreadGroupForm :config="threadGroupConfig" :node="threadGroupNode"
          :number-value="threadGroupNumberValue" :can-edit="canEditNumber" />

        <CsvDatasetPanel :config="csvDatasetConfig" />

        <EndpointList
          :endpoints="endpointList"
          :selected-id="selectedEndpointId"
          @select="selectEndpoint"
          @add="addEndpoint"
          @delete="deleteEndpoint"
          @duplicate="duplicateEndpoint"
          @reorder="handleReorder"
        />
      </div>

      <!-- Center: Apifox-style main workspace -->
      <div class="center-panel">
        <div v-if="!selectedEndpoint" class="no-selection">
          <el-empty :description="t('editor.selectEndpoint')" :image-size="64" />
        </div>

        <template v-else>
          <!-- Primary view tabs (Apifox style) -->
          <div class="main-view-bar">
            <div class="main-view-tabs">
              <span
                v-for="view in mainViews"
                :key="view.name"
                :class="['main-view-tab', { active: mainView === view.name }]"
                @click="mainView = view.name"
              >
                {{ view.label }}
              </span>
            </div>
            <div class="main-view-actions">
              <el-button size="small" @click="handleSave" :loading="saving">{{ t('editor.save') }}</el-button>
              <el-button v-if="mainView === 'debug'" type="primary" size="small" @click="handleSend" :loading="sending">
                {{ t('editor.send') }}
              </el-button>
            </div>
          </div>

          <!-- Edit: API definition -->
          <div v-show="mainView === 'edit'" class="view-panel edit-view">
            <div class="edit-header">
              <el-input
                v-model="selectedEndpoint.name"
                class="endpoint-title-input"
                :placeholder="t('editor.endpointName')"
              />
              <div class="edit-meta">
                <el-tag size="small" type="warning">{{ t('editor.inDevelopment') }}</el-tag>
                <el-tag size="small">{{ selectedEndpoint.method }}</el-tag>
              </div>
            </div>
            <div class="edit-description">
              <el-input
                v-model="selectedEndpoint.description"
                type="textarea"
                :rows="3"
                :placeholder="t('editor.endpointDescription')"
              />
            </div>
            <div class="edit-request-preview">
              <div class="section-label">{{ t('editor.requestDefinition') }}</div>
              <div class="request-url-bar preview-bar">
                <el-select v-model="selectedEndpoint.method" class="method-select" size="small">
                  <el-option v-for="m in httpMethods" :key="m" :label="m" :value="m" />
                </el-select>
                <div class="url-input-group">
                  <span class="url-prefix">{{ activeEnv?.base_url || '${base_url}' }}</span>
                  <el-input v-model="selectedEndpoint.path" placeholder="/api/v1/endpoint" class="url-input" size="small" />
                </div>
              </div>
              <el-form label-position="top" size="small" class="edit-body-form">
                <el-form-item :label="t('editor.bodyType')">
                  <el-radio-group v-model="selectedEndpoint.bodyType" class="body-type-group">
                    <el-radio v-for="t in bodyTypes" :key="t.value" :value="t.value">{{ t.label }}</el-radio>
                  </el-radio-group>
                </el-form-item>
                <el-form-item v-if="selectedEndpoint.bodyType !== 'none'" :label="t('editor.bodyStructure')">
                  <el-input
                    v-model="selectedEndpoint.bodyContent"
                    type="textarea"
                    :rows="12"
                    class="body-textarea"
                    :placeholder="bodyPlaceholder"
                  />
                </el-form-item>
              </el-form>
            </div>
          </div>

          <!-- Debug: request + response split -->
          <div v-show="mainView === 'debug'" class="view-panel debug-view">
            <div class="request-url-bar">
              <el-select v-model="selectedEndpoint.method" class="method-select" size="small">
                <el-option v-for="m in httpMethods" :key="m" :label="m" :value="m" />
              </el-select>
              <div class="url-input-group">
                <span class="url-prefix">{{ activeEnv?.base_url || '${base_url}' }}</span>
                <el-input v-model="selectedEndpoint.path" placeholder="/api/v1/endpoint" class="url-input" size="small" />
              </div>
              <el-button type="primary" size="small" @click="handleSend" :loading="sending">{{ t('editor.send') }}</el-button>
            </div>

            <div class="debug-split">
              <div class="debug-request">
                <el-tabs v-model="debugTab" class="request-tabs">
                  <el-tab-pane name="params">
                    <template #label>Params <span v-if="paramCount" class="tab-badge">{{ paramCount }}</span></template>
                    <div class="tab-content">
                      <ParamsEditor :params="selectedEndpoint.queryParams" />
                    </div>
                  </el-tab-pane>

                  <el-tab-pane name="body">
                    <template #label>Body <span v-if="hasBody" class="tab-badge">1</span></template>
                    <div class="tab-content">
                      <el-radio-group v-model="selectedEndpoint.bodyType" class="body-type-group" size="small">
                        <el-radio v-for="t in bodyTypes" :key="t.value" :value="t.value">{{ t.label }}</el-radio>
                      </el-radio-group>
                      <el-input
                        v-if="selectedEndpoint.bodyType !== 'none'"
                        v-model="selectedEndpoint.bodyContent"
                        type="textarea"
                        :rows="10"
                        class="body-textarea"
                        :placeholder="bodyPlaceholder"
                        style="margin-top: 12px"
                      />
                    </div>
                  </el-tab-pane>

                  <el-tab-pane name="headers">
                    <template #label>Headers <span v-if="headerCount" class="tab-badge">{{ headerCount }}</span></template>
                    <div class="tab-content">
                      <HeadersEditor v-model="headerProps" />
                    </div>
                  </el-tab-pane>

                  <el-tab-pane name="cookies">
                    <template #label>Cookies <span v-if="cookieCount" class="tab-badge">{{ cookieCount }}</span></template>
                    <div class="tab-content">
                      <CookiesEditor v-model="cookieProps" />
                    </div>
                  </el-tab-pane>

                  <el-tab-pane name="auth">
                    <template #label>{{ t('editor.authType') }}</template>
                    <div class="tab-content">
                      <AuthSettings :auth="selectedEndpoint.auth" />
                    </div>
                  </el-tab-pane>
                </el-tabs>
              </div>
              <div class="debug-response">
                <ResponsePanel :response="debugResponse" />
              </div>
            </div>
          </div>

          <!-- Test case: scripts & assertions -->
          <div v-show="mainView === 'testcase'" class="view-panel testcase-view">
            <el-tabs v-model="testcaseTab" class="request-tabs">
              <el-tab-pane :label="t('editor.preActions')" name="preScript">
                <div class="tab-content">
                  <PreProcessorEditor
                    v-model="selectedEndpoint.preScript"
                    @open-helper="functionHelperVisible = true"
                  />
                </div>
              </el-tab-pane>
              <el-tab-pane :label="t('editor.postActions')" name="postScript">
                <div class="tab-content">
                  <PostProcessorEditor
                    v-model="selectedEndpoint.postScript"
                    @add-extractor="handleAddExtractor"
                  />
                </div>
              </el-tab-pane>
              <el-tab-pane name="assertions">
                <template #label>{{ t('editor.assertions') }} <span v-if="assertionCount" class="tab-badge">{{ assertionCount }}</span></template>
                <div class="tab-content">
                  <AssertionsTab v-model="selectedEndpoint.assertions" />
                </div>
              </el-tab-pane>
            </el-tabs>
          </div>

          <!-- Settings -->
          <div v-show="mainView === 'settings'" class="view-panel settings-view">
            <EndpointSettingsForm :endpoint="selectedEndpoint" :can-edit="canEditNumber" />
          </div>
        </template>
      </div>
    </div>
    <JmxTreeEditor v-else ref="treeEditorRef" :plan-id="planId" class="tree-mode" />

    <!-- Function Helper Dialog -->
    <FunctionHelper v-model:visible="functionHelperVisible" @insert="handleFunctionInsert" />

    <!-- JMX Source Dialog -->
    <el-dialog v-model="sourceDialogVisible" :title="t('editor.jmxSource')" width="800px" style="max-width: calc(100vw - 32px);">
      <el-input v-model="jmxSource" type="textarea" :rows="24" readonly />
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, reactive, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  ArrowLeft, DocumentChecked, VideoPlay, Document
} from '@element-plus/icons-vue'
import { api } from '@/api'
import type { EnvironmentInfo } from '@/store'
import { useI18n } from '@/composables/useI18n'
import EnvironmentSelector from '@/components/editor/EnvironmentSelector.vue'
import EndpointList from '@/components/editor/EndpointList.vue'
import PreProcessorEditor from '@/components/editor/PreProcessorEditor.vue'
import PostProcessorEditor from '@/components/editor/PostProcessorEditor.vue'
import AssertionsTab from '@/components/editor/AssertionsTab.vue'
import HeadersEditor from '@/components/editor/HeadersEditor.vue'
import CookiesEditor from '@/components/editor/CookiesEditor.vue'
import ResponsePanel from '@/components/editor/ResponsePanel.vue'
import FunctionHelper from '@/components/editor/FunctionHelper.vue'
import JmxTreeEditor from '@/components/editor/JmxTreeEditor.vue'
import CsvDatasetPanel from '@/components/editor/CsvDatasetPanel.vue'
import AuthSettings from '@/components/editor/AuthSettings.vue'
import ThreadGroupForm from '@/components/editor/ThreadGroupForm.vue'
import EndpointSettingsForm from '@/components/editor/EndpointSettingsForm.vue'
import ParamsEditor from '@/components/editor/ParamsEditor.vue'
import type { DebugResponse } from '@/components/editor/ResponsePanel.vue'
import { usePlanTree, type EndpointData } from '@/composables/usePlanTree'
import { useEndpointList } from '@/composables/useEndpointList'

const httpMethods = ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS']
const bodyTypes = [
  { value: 'none', label: 'none' },
  { value: 'json', label: 'JSON' },
  { value: 'form-data', label: 'form-data' },
  { value: 'x-www-form-urlencoded', label: 'x-www-form-urlencoded' },
  { value: 'raw', label: 'raw' },
]

type MainView = 'edit' | 'debug' | 'testcase' | 'settings'

const route = useRoute()
const router = useRouter()
const { t, L } = useI18n()

const planId = computed(() => route.params.planId as string || route.params.id as string)
const projectId = computed(() => route.params.projectId as string)

const mainViews = computed(() => [
  { name: 'edit' as const, label: t('editor.view.edit') },
  { name: 'debug' as const, label: t('editor.view.debug') },
  { name: 'testcase' as const, label: t('editor.view.testcase') },
  { name: 'settings' as const, label: t('editor.view.settings') },
])

const planName = ref('')
const projectName = ref('')
const saving = ref(false)
const running = ref(false)
const sending = ref(false)
const mainView = ref<MainView>('edit')
const debugTab = ref('params')
const testcaseTab = ref('preScript')
const debugResponse = ref<DebugResponse | null>(null)

// Tree data (raw JMX tree)
const treeData = ref<any>(null)
const threadGroupNode = ref<any>(null)

// Endpoints parsed from tree
const endpoints = ref<Map<string, any>>(new Map())
const selectedEndpointId = ref<string | null>(null)
const selectedEndpoint = ref<EndpointData | null>(null)

// Environment
const envList = ref<EnvironmentInfo[]>([])
const activeEnvId = ref<string | null>(null)
const activeEnv = ref<EnvironmentInfo | null>(null)

// Dialogs
const functionHelperVisible = ref(false)
const sourceDialogVisible = ref(false)
const jmxSource = ref('')

// Thread Group configuration
const threadGroupConfig = reactive({
  numThreads: 1,
  rampTime: 1,
  loops: 1,
  duration: 0,
  delay: 0,
  scheduler: false,
  onSampleError: 'continue',
})

// CSV Data Set configuration
const csvDatasetConfig = reactive({
  filePath: '',
  variableNames: '',
  delimiter: ',',
  encoding: 'UTF-8',
  ignoreFirstLine: false,
  recycle: true,
})


const {
  isThreadGroup, isCsvDataset, readThreadGroupConfig, threadGroupNumberValue, canEditNumber, readCsvConfig, csvHolder,
  parseEndpoint, buildTreeFromEndpoints,
} = usePlanTree({ planId, treeData, threadGroupNode, endpoints, threadGroupConfig, csvDatasetConfig, t })

// The request list: selection, add, copy, remove, reorder.
const { endpointList, selectEndpoint, addEndpoint, deleteEndpoint, duplicateEndpoint, reorderEndpoint: handleReorder } =
  useEndpointList({
    endpoints, selectedId: selectedEndpointId, t, L, notify: ElMessage,
    onSelect: () => { mainView.value = 'edit'; debugTab.value = 'params' },
  })

const headerProps = computed({
  get: () => {
    const ep = selectedEndpoint.value
    if (!ep) return {}
    const list = ep.headers.map((h: any) => ({
      _elementType: 'Header',
      _name: h.name || '',
      'Header.name': h.name || '',
      'Header.value': h.value || '',
    }))
    return { 'HeaderManager.headers': list }
  },
  set: (val: any) => {
    if (!selectedEndpoint.value) return
    const headersList = val['HeaderManager.headers'] || []
    selectedEndpoint.value.headers = headersList.map((h: any) => ({
      name: h['Header.name'] || '',
      value: h['Header.value'] || '',
    }))
  },
})

const bodyPlaceholder = computed(() => {
  const type = selectedEndpoint.value?.bodyType
  if (type === 'json') return '{\n  "key": "value"\n}'
  if (type === 'raw') return t('editor.rawBodyHint')
  return t('editor.bodyHint')
})

const paramCount = computed(() =>
  selectedEndpoint.value?.queryParams.filter(p => p.name).length || 0
)
const headerCount = computed(() =>
  selectedEndpoint.value?.headers.filter(h => h.name).length || 0
)
const cookieCount = computed(() =>
  selectedEndpoint.value?.cookies?.filter(c => c.name).length || 0
)
const assertionCount = computed(() => selectedEndpoint.value?.assertions.length || 0)
const hasBody = computed(() =>
  selectedEndpoint.value?.bodyType !== 'none' && !!selectedEndpoint.value?.bodyContent
)

const cookieProps = computed({
  get: () => {
    const ep = selectedEndpoint.value
    if (!ep) return {}
    const list = (ep.cookies || []).map((c: any) => ({
      _elementType: 'Cookie',
      _name: c.name || '',
      'Cookie.name': c.name || '',
      'Cookie.value': c.value || '',
      'Cookie.domain': c.domain || '',
      'Cookie.path': c.path || '/',
    }))
    return { 'CookieManager.cookies': list }
  },
  set: (val: any) => {
    if (!selectedEndpoint.value) return
    const cookiesList = val['CookieManager.cookies'] || []
    selectedEndpoint.value.cookies = cookiesList.map((c: any) => ({
      name: c['Cookie.name'] || '',
      value: c['Cookie.value'] || '',
      domain: c['Cookie.domain'] || '',
      path: c['Cookie.path'] || '/',
    }))
  },
})

// === Form / JMX tree views ===
// Both views edit the saved plan: switching saves the current view if it changed, and the other reloads.
const viewMode = ref<'form' | 'tree'>('form')
const switchingView = ref(false)
const treeEditorRef = ref<InstanceType<typeof JmxTreeEditor> | null>(null)
const viewModeOptions = computed(() => [
  { label: t('editor.view.form'), value: 'form' },
  { label: t('editor.view.tree'), value: 'tree' },
])

async function switchView(mode: string | number | boolean) {
  if (mode === viewMode.value) return
  switchingView.value = true
  try {
    if (mode === 'tree') {
      const unchanged = JSON.stringify(buildTreeFromEndpoints()) === JSON.stringify(treeData.value)
      if (unchanged || await handleSave()) viewMode.value = 'tree'
    } else {
      const tree = treeEditorRef.value
      if (tree && tree.isDirty() && !await tree.handleSave()) return
      await loadPlan()
      viewMode.value = 'form'
    }
  } finally {
    switchingView.value = false
  }
}

// === Lifecycle ===
onMounted(async () => {
  await loadPlan()
  if (projectId.value) {
    await loadEnvironments()
    await loadProject()
  }
})

watch(selectedEndpointId, (newId) => {
  if (newId && endpoints.value.has(newId)) {
    selectedEndpoint.value = endpoints.value.get(newId)
  } else if (!newId) {
    selectedEndpoint.value = null
  }
})

// === Data Loading ===
async function loadPlan() {
  try {
    const res = await api.parseTestPlan(planId.value)
    treeData.value = res.data
    if (res.data) {
      planName.value = res.data.testname || t('editor.defaultPlanName')
      parseTreeToEndpoints(res.data)
    }
  } catch (e: any) {
    ElMessage.error(t('editor.message.loadPlanFailed', { msg: e.response?.data?.detail || e.message }))
  }
}

async function loadEnvironments() {
  try {
    const res = await api.getProjectEnvironments(projectId.value)
    if (res.data?.environments) {
      envList.value = res.data.environments
    }
    if (res.data?.active_id) {
      activeEnvId.value = res.data.active_id
      const found = envList.value.find(e => e.id === res.data.active_id)
      activeEnv.value = found || null
    } else if (envList.value.length > 0) {
      activeEnvId.value = envList.value[0].id
      activeEnv.value = envList.value[0]
    }
  } catch {
    envList.value = []
  }
}

async function loadProject() {
  try {
    const res = await api.getProject(projectId.value)
    projectName.value = res.data?.name || ''
  } catch { /* ignore */ }
}

// === Tree Parsing ===
function parseTreeToEndpoints(tree: any) {
  endpoints.value = new Map()
  threadGroupNode.value = null

  // Keep each sampler's original parent, including controllers and other groups.
  function walk(node: any, parentId?: string) {
    const tc = node.testclass
    if (!threadGroupNode.value && isThreadGroup(node)) {
      threadGroupNode.value = node
    }
    if (tc === 'HTTPSamplerProxy') {
      const endpoint = parseEndpoint(node)
      endpoint._sourceId = node.id
      endpoint._parentId = parentId
      endpoints.value.set(endpoint.id, endpoint)
    }
    if (node.children) {
      for (const child of node.children) {
        walk(child, node.id)
      }
    }
  }

  walk(tree)
  Object.assign(threadGroupConfig, readThreadGroupConfig(threadGroupNode.value))
  Object.assign(csvDatasetConfig, readCsvConfig(csvHolder(tree, threadGroupNode.value)?.children?.find(isCsvDataset)))

  // Select first endpoint
  if (endpoints.value.size > 0) {
    const firstKey = endpoints.value.keys().next().value as string
    if (firstKey) selectEndpoint(firstKey)
  }
}

// === Handlers ===
function handleEnvChange(envId: string) {
  activeEnvId.value = envId
  const found = envList.value.find(e => e.id === envId)
  activeEnv.value = found || null
  if (projectId.value) {
    api.setActiveEnvironment(projectId.value, envId).catch(() => {})
  }
}

async function handleCopyEnv(env: any) {
  if (!projectId.value) return
  try {
    const newEnv = {
      name: env.name + t('project.copySuffix'),
      base_url: env.base_url,
      variables: env.variables || {},
      headers: env.headers || {}
    }
    await api.createEnvironment(projectId.value, newEnv)
    await loadEnvironments()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || t('project.message.copyFailed'))
  }
}

function handleAddExtractor(extractor: any) {
  if (!selectedEndpoint.value) return
  if (!selectedEndpoint.value.extractors) selectedEndpoint.value.extractors = []
  selectedEndpoint.value.extractors.push(extractor)
}

async function handleFunctionInsert(funcString: string) {
  try {
    await navigator.clipboard.writeText(funcString)
    ElMessage.success(t('editor.message.functionCopied', { func: funcString }))
  } catch {
    const input = document.createElement('input')
    input.value = funcString
    document.body.appendChild(input)
    input.select()
    document.execCommand('copy')
    document.body.removeChild(input)
    ElMessage.success(t('editor.message.functionCopied', { func: funcString }))
  }
}

async function handleSend() {
  if (!selectedEndpoint.value) return
  sending.value = true
  debugResponse.value = null
  mainView.value = 'debug'
  try {
    ElMessage.info(t('editor.message.singleEndpointNotSupported'))
  } finally {
    sending.value = false
  }
}

// === Save / Run ===
async function handleSave() {
  saving.value = true
  try {
    const tree = buildTreeFromEndpoints()
    await api.saveTestPlanTree(planId.value, { tree })
    // Refresh local state
    treeData.value = tree
    ElMessage.success(t('editor.message.saveSuccess'))
    return true
  } catch (e: any) {
    ElMessage.error(t('editor.message.saveFailed', { msg: e.response?.data?.detail || e.message }))
    return false
  } finally {
    saving.value = false
  }
}

async function handleRun() {
  running.value = true
  try {
    if (!await handleSave()) return
    await api.executeTest(planId.value)
    ElMessage.success(t('editor.message.runStarted'))
    router.push('/executions')
  } catch (e: any) {
    ElMessage.error(t('editor.message.runFailed', { msg: e.response?.data?.detail || e.message }))
  } finally {
    running.value = false
  }
}

async function handleViewSource() {
  try {
    const res = await api.getJmxContent(planId.value)
    jmxSource.value = res.data?.content || ''
    sourceDialogVisible.value = true
  } catch (e: any) {
    ElMessage.error(t('editor.message.loadJmxFailed', { msg: e.response?.data?.detail || e.message }))
  }
}

function goBack() {
  if (projectId.value) {
    router.push(`/projects/${projectId.value}/plans`)
  } else {
    router.push('/projects')
  }
}

function goToProjectSettings() {
  if (projectId.value) {
    router.push(`/projects/${projectId.value}/settings?section=environments`)
  }
}
</script>

<style scoped lang="scss" src="./TestCaseEditor.scss"></style>
