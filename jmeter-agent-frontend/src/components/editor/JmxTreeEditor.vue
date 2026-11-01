<template>
  <div class="editor-page">
    <div class="editor-toolbar">
      <div class="toolbar-center">
        <el-button type="primary" @click="handleSave" :loading="saving">
          <el-icon><DocumentChecked /></el-icon> {{ t('jmxEditor.save') }}
        </el-button>
        <el-button type="primary" @click="handleRun" :loading="running">
          <el-icon><VideoPlay /></el-icon> {{ t('jmxEditor.run') }}
        </el-button>
        <el-button @click="handleAddChild" :disabled="!canAddChild">
          <el-icon><Plus /></el-icon> {{ t('jmxEditor.addComponent') }}
        </el-button>
        <el-button @click="handleCopy" :disabled="!selectedNode || selectedNode.testclass === 'TestPlan'">
          <el-icon><CopyDocument /></el-icon> {{ t('jmxEditor.copy') }}
        </el-button>
        <el-button @click="handleDelete" :disabled="!selectedNode || selectedNode.testclass === 'TestPlan'">
          <el-icon><Delete /></el-icon> {{ t('jmxEditor.delete') }}
        </el-button>
        <el-button @click="handleToggleEnable" :disabled="!selectedNode || selectedNode.testclass === 'TestPlan'">
          <el-icon><SwitchButton /></el-icon> {{ selectedNode?.enabled !== false ? t('jmxEditor.disable') : t('jmxEditor.enable') }}
        </el-button>
        <el-button @click="functionHelperVisible = true">
          <span class="fx-label">fx</span> {{ t('jmxEditor.functionHelper') }}
        </el-button>
        <el-button @click="handleViewSource">
          <el-icon><Document /></el-icon> {{ t('jmxEditor.viewJmx') }}
        </el-button>
      </div>
    </div>

    <div class="editor-body">
      <div class="tree-panel">
        <div class="panel-header">{{ t('jmxEditor.componentTree') }}</div>
        <el-tree
          ref="treeRef"
          :data="treeData"
          :props="{ children: 'children', label: 'testname' }"
          node-key="id"
          highlight-current
          :expand-on-click-node="false"
          default-expand-all
          @node-click="handleNodeClick"
        >
          <template #default="{ data }">
            <span class="tree-node" :class="{ disabled: data.enabled === false }">
              <span class="node-icon">{{ getComponentIcon(data.testclass) }}</span>
              <span class="node-label">{{ data.testname || getComponentLabel(data.testclass) }}</span>
              <el-tag v-if="data.enabled === false" size="small" type="info">{{ t('jmxEditor.disabledTag') }}</el-tag>
            </span>
          </template>
        </el-tree>
      </div>

      <div class="property-panel">
        <div class="panel-header">
          {{ t('jmxEditor.properties') }}
          <el-tag v-if="selectedNode" size="small" type="info" style="margin-left: 8px">
            {{ getComponentLabel(selectedNode.testclass) }}
          </el-tag>
        </div>
        <div v-if="selectedNode" class="property-content">
          <el-form label-position="top">
            <el-form-item :label="t('jmxEditor.name')">
              <el-input v-model="selectedNode.testname" />
            </el-form-item>

            <template v-if="selectedMeta">
              <template v-for="(propDef, propKey) in selectedMeta.properties" :key="propKey">
                <el-form-item v-if="!String(propKey).startsWith('__')" :label="propDef.label">
                  <template v-if="propDef.type === 'text'">
                    <FunctionInput v-model="selectedNode.properties[propKey]" />
                  </template>
                  <template v-else-if="propDef.type === 'textarea'">
                    <FunctionTextarea v-model="selectedNode.properties[propKey]" />
                  </template>
                  <template v-else-if="propDef.type === 'number'">
                    <el-input-number
                      v-model="selectedNode.properties[propKey]"
                      :min="String(propKey) === 'loops' ? -1 : 0"
                      style="width: 100%"
                      @change="(val: any) => selectedNode.properties[propKey] = val"
                    />
                  </template>
                  <template v-else-if="propDef.type === 'switch'">
                    <el-switch v-model="selectedNode.properties[propKey]" />
                  </template>
                  <template v-else-if="propDef.type === 'select'">
                    <el-select
                      v-model="selectedNode.properties[propKey]"
                      style="width: 100%"
                      @change="(val: string | number) => onSelectChange(String(propKey), val)"
                    >
                      <el-option
                        v-for="opt in propDef.options"
                        :key="opt.value"
                        :label="opt.label"
                        :value="opt.value"
                      />
                    </el-select>
                  </template>
                </el-form-item>
              </template>

              <HttpParamsEditor
                v-if="selectedNode.testclass === 'HTTPSamplerProxy' && !selectedNode.properties['HTTPSampler.postBodyRaw']"
                v-model="selectedNode.properties"
              />
              <el-form-item
                v-if="selectedNode.testclass === 'HTTPSamplerProxy' && selectedNode.properties['HTTPSampler.postBodyRaw']"
                :label="t('jmxEditor.bodyData')"
              >
                <FunctionTextarea v-model="selectedNode.properties['__body_data']" :rows="8" />
              </el-form-item>

              <HeadersEditor v-if="selectedMeta.special_editor === 'headers'" v-model="selectedNode.properties" />
              <VariablesEditor v-if="selectedMeta.special_editor === 'variables'" v-model="selectedNode.properties" />
              <AssertionPatternsEditor v-if="selectedMeta.special_editor === 'assertion_patterns'" v-model="selectedNode.properties" />
              <CookiesEditor v-if="selectedMeta.special_editor === 'cookies'" v-model="selectedNode.properties" />
              <AuthEditor v-if="selectedMeta.special_editor === 'auth'" v-model="selectedNode.properties" />
              <UserParametersEditor v-if="selectedMeta.special_editor === 'user_parameters'" v-model="selectedNode.properties" />
              <DnsHostsEditor v-if="selectedMeta.special_editor === 'dns_hosts'" v-model="selectedNode.properties" />
            </template>

            <template v-else>
              <el-form-item :label="t('jmxEditor.rawProperties')">
                <el-input v-model="rawPropertiesText" type="textarea" :rows="10" @blur="applyRawProperties" />
              </el-form-item>
            </template>
          </el-form>
        </div>
        <el-empty v-else :description="t('jmxEditor.selectComponent')" />
      </div>
    </div>

    <el-dialog v-model="addDialogVisible" :title="t('jmxEditor.addDialog')" width="520px" style="max-width: calc(100vw - 32px);">
      <el-form label-position="top">
        <el-form-item :label="t('jmxEditor.selectType')">
          <el-select v-model="selectedAddComponent" filterable style="width: 100%">
            <el-option-group v-for="group in addComponentGroups" :key="group.category" :label="group.label">
              <el-option v-for="comp in group.components" :key="comp.testclass" :label="comp.label" :value="comp.testclass">
                <span>{{ comp.icon }} {{ comp.label }}</span>
              </el-option>
            </el-option-group>
          </el-select>
        </el-form-item>
        <el-form-item :label="t('jmxEditor.componentName')">
          <el-input v-model="addComponentName" :placeholder="t('jmxEditor.namePlaceholder')" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addDialogVisible = false">{{ t('jmxEditor.cancel') }}</el-button>
        <el-button type="primary" @click="confirmAddChild">{{ t('jmxEditor.confirm') }}</el-button>
      </template>
    </el-dialog>

    <FunctionHelper v-model:visible="functionHelperVisible" @insert="handleGlobalFunctionInsert" />

    <el-dialog v-model="sourceDialogVisible" :title="t('jmxEditor.jmxSourceDialog')" width="800px" style="max-width: calc(100vw - 32px);">
      <el-input v-model="jmxSource" type="textarea" :rows="24" readonly />
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CopyDocument, Delete, Document, DocumentChecked, Plus, VideoPlay, SwitchButton } from '@element-plus/icons-vue'
import { useI18n } from '@/composables/useI18n'
import { api } from '@/api/index'
import FunctionInput from '@/components/editor/FunctionInput.vue'
import FunctionTextarea from '@/components/editor/FunctionTextarea.vue'
import FunctionHelper from '@/components/editor/FunctionHelper.vue'
import HeadersEditor from '@/components/editor/HeadersEditor.vue'
import VariablesEditor from '@/components/editor/VariablesEditor.vue'
import AssertionPatternsEditor from '@/components/editor/AssertionPatternsEditor.vue'
import CookiesEditor from '@/components/editor/CookiesEditor.vue'
import AuthEditor from '@/components/editor/AuthEditor.vue'
import UserParametersEditor from '@/components/editor/UserParametersEditor.vue'
import DnsHostsEditor from '@/components/editor/DnsHostsEditor.vue'
import HttpParamsEditor from '@/components/editor/HttpParamsEditor.vue'

const { t } = useI18n()

const LISTENER_LABELS: Record<string, string> = {
  ViewResultsFullVisualizer: 'View Results Tree',
  SummaryReport: 'Summary Report',
  StatVisualizer: 'Aggregate Report',
  GraphVisualizer: 'Graph Results',
  RespTimeGraphVisualizer: 'Response Time Graph',
  TableVisualizer: 'View Results in Table',
  SimpleDataWriter: 'Simple Data Writer',
  AssertionVisualizer: 'Assertion Results',
  MailerVisualizer: 'Mailer Visualizer',
  BackendListenerGui: 'Backend Listener',
}

const props = defineProps<{ planId: string }>()
const router = useRouter()
const planId = props.planId

const treeData = ref<any[]>([])
const selectedNode = ref<any>(null)
const treeRef = ref<any>(null)
const saving = ref(false)
const running = ref(false)

const addDialogVisible = ref(false)
const selectedAddComponent = ref('')
const addComponentName = ref('')
const allowedChildren = ref<any[]>([])

const componentMetaMap = ref<Record<string, any>>({})
const rawPropertiesText = ref('')

const functionHelperVisible = ref(false)
const sourceDialogVisible = ref(false)
const jmxSource = ref('')

const selectedMeta = computed(() => {
  if (!selectedNode.value) return null
  return componentMetaMap.value[selectedNode.value.testclass]
})

const canAddChild = computed(() => {
  if (!selectedNode.value) return false
  const meta = componentMetaMap.value[selectedNode.value.testclass]
  return meta && meta.allowed_children && meta.allowed_children.length > 0
})

const categoryLabels = computed<Record<string, string>>(() => ({
  plan: t('jmxEditor.type.testPlan'),
  thread: t('jmxEditor.type.thread'),
  sampler: t('jmxEditor.type.sampler'),
  controller: t('jmxEditor.type.logicController'),
  config: t('jmxEditor.type.config'),
  assertion: t('jmxEditor.type.assertion'),
  extractor: t('jmxEditor.type.extractor'),
  pre_processor: t('jmxEditor.type.preProcessor'),
  post_processor: t('jmxEditor.type.postProcessor'),
  timer: t('jmxEditor.type.timer'),
  listener: t('jmxEditor.type.listener'),
  other: t('jmxEditor.type.other'),
}))

const addComponentGroups = computed(() => {
  const groups: Record<string, any[]> = {}
  for (const comp of allowedChildren.value) {
    const cat = comp.category || 'other'
    if (!groups[cat]) groups[cat] = []
    groups[cat].push(comp)
  }
  return Object.entries(groups).map(([category, components]) => ({
    category,
    label: categoryLabels.value[category] || category,
    components,
  }))
})

function getComponentIcon(testclass: string) {
  return componentMetaMap.value[testclass]?.icon || '⚙️'
}

function getComponentLabel(testclass: string) {
  return componentMetaMap.value[testclass]?.label || testclass
}

function findNodePath(nodes: any[], id: string, path: any[] = []): any[] | null {
  for (const node of nodes) {
    if (node.id === id) return [...path, node]
    if (node.children) {
      const found = findNodePath(node.children, id, [...path, node])
      if (found) return found
    }
  }
  return null
}

function removeNodeById(nodes: any[], id: string): boolean {
  const idx = nodes.findIndex((n) => n.id === id)
  if (idx >= 0) {
    nodes.splice(idx, 1)
    return true
  }
  for (const node of nodes) {
    if (node.children && removeNodeById(node.children, id)) return true
  }
  return false
}

function deepCloneNode(node: any): any {
  return {
    ...node,
    id: generateId(),
    properties: JSON.parse(JSON.stringify(node.properties || {})),
    children: (node.children || []).map(deepCloneNode),
  }
}

function coerceValue(value: any, targetType: string): any {
  if (targetType === 'number') {
    const n = Number(value)
    return isNaN(n) ? 0 : n
  }
  if (targetType === 'switch') {
    return Boolean(value)
  }
  return value ?? ''
}

function ensureProperties(node: any) {
  const meta = componentMetaMap.value[node.testclass]
  if (!node.properties) node.properties = {}
  if (meta?.defaults) {
    for (const [key, def] of Object.entries(meta.defaults)) {
      if (node.properties[key] === undefined) {
        node.properties[key] = def
      }
    }
  }
  if (meta?.properties) {
    for (const [key, propDef] of Object.entries(meta.properties)) {
      const pd = propDef as any
      if (node.properties[key] !== undefined && pd.type) {
        node.properties[key] = coerceValue(node.properties[key], pd.type)
      }
    }
  }
}

function onSelectChange(propKey: string, value: string | number) {
  if (!selectedNode.value) return
  if (propKey === '_listener_type' && selectedNode.value.testclass === 'ResultCollector') {
    selectedNode.value.testname = LISTENER_LABELS[value] || selectedNode.value.testname
  }
}

function handleNodeClick(data: any) {
  selectedNode.value = data
  ensureProperties(data)
  if (!selectedMeta.value && data.properties) {
    rawPropertiesText.value = JSON.stringify(data.properties, null, 2)
  }
}

function handleAddChild() {
  if (!selectedNode.value) return
  api.getComponentChildren(selectedNode.value.testclass).then((res: any) => {
    allowedChildren.value = res.data || []
    selectedAddComponent.value = allowedChildren.value[0]?.testclass || ''
    addComponentName.value = ''
    addDialogVisible.value = true
  })
}

function confirmAddChild() {
  if (!selectedAddComponent.value || !selectedNode.value) return
  const meta = componentMetaMap.value[selectedAddComponent.value]
  const newNode: any = {
    id: generateId(),
    testclass: selectedAddComponent.value,
    testname: addComponentName.value || meta?.label || selectedAddComponent.value,
    enabled: true,
    properties: { ...(meta?.defaults || {}) },
    children: [],
  }
  if (!selectedNode.value.children) selectedNode.value.children = []
  selectedNode.value.children.push(newNode)
  addDialogVisible.value = false
  handleNodeClick(newNode)
}

function handleCopy() {
  if (!selectedNode.value) return
  const path = findNodePath(treeData.value, selectedNode.value.id)
  if (!path || path.length < 2) return
  const parent = path[path.length - 2]
  if (!parent.children) parent.children = []
  const copy = deepCloneNode(selectedNode.value)
  copy.testname = copy.testname + t('jmxEditor.copySuffix')
  parent.children.push(copy)
  handleNodeClick(copy)
  ElMessage.success(t('jmxEditor.copied'))
}

function handleDelete() {
  if (!selectedNode.value) return
  ElMessageBox.confirm(t('jmxEditor.confirmDelete'), t('jmxEditor.deletePrompt'), { type: 'warning' }).then(() => {
    removeNodeById(treeData.value, selectedNode.value.id)
    selectedNode.value = null
    ElMessage.success(t('jmxEditor.deleted'))
  })
}

function handleToggleEnable() {
  if (!selectedNode.value) return
  selectedNode.value.enabled = selectedNode.value.enabled === false
}

async function handleGlobalFunctionInsert(funcString: string) {
  try {
    await navigator.clipboard.writeText(funcString)
    ElMessage.success(t('jmxEditor.functionCopied', { func: funcString }))
  } catch {
    // 降级：创建临时输入框复制
    const input = document.createElement('input')
    input.value = funcString
    document.body.appendChild(input)
    input.select()
    document.execCommand('copy')
    document.body.removeChild(input)
    ElMessage.success(t('jmxEditor.functionCopied', { func: funcString }))
  }
}

function applyRawProperties() {
  try {
    if (selectedNode.value && rawPropertiesText.value) {
      selectedNode.value.properties = JSON.parse(rawPropertiesText.value)
      ElMessage.success(t('jmxEditor.propertiesUpdated'))
    }
  } catch {
    ElMessage.error(t('jmxEditor.jsonError'))
  }
}

async function handleSave() {
  saving.value = true
  try {
    await api.saveTestPlanTree(planId, { tree: treeData.value[0] })
    savedSnapshot = JSON.stringify(treeData.value[0])
    ElMessage.success(t('jmxEditor.saveSuccess'))
    return true
  } catch (e: any) {
    ElMessage.error(t('jmxEditor.saveFailed', { msg: e.response?.data?.detail || e.message }))
    return false
  } finally {
    saving.value = false
  }
}

async function handleRun() {
  running.value = true
  try {
    if (!await handleSave()) return
    await api.executeTest(planId)
    ElMessage.success(t('jmxEditor.runStarted'))
    router.push('/executions')
  } catch (e: any) {
    ElMessage.error(t('jmxEditor.runFailed', { msg: e.response?.data?.detail || e.message }))
  } finally {
    running.value = false
  }
}

async function handleViewSource() {
  try {
    const res = await api.getJmxContent(planId)
    jmxSource.value = res.data?.content || ''
    sourceDialogVisible.value = true
  } catch (e: any) {
    ElMessage.error(t('jmxEditor.loadFailed', { msg: e.response?.data?.detail || e.message }))
  }
}

// Lets the parent editor skip a save (and its JMX rewrite) when nothing changed.
let savedSnapshot = ''
const isDirty = () => JSON.stringify(treeData.value[0]) !== savedSnapshot

defineExpose({ handleSave, isDirty })

function generateId() {
  return Math.random().toString(36).substring(2, 10)
}

onMounted(async () => {
  try {
    const compRes = await api.getComponents()
    for (const c of compRes.data || []) {
      componentMetaMap.value[c.testclass] = c
    }

    const parseRes = await api.parseTestPlan(planId)
    const tree = parseRes.data
    if (tree) {
      treeData.value = [tree]
      savedSnapshot = JSON.stringify(tree)
    }
  } catch (e: any) {
    ElMessage.error(t('jmxEditor.loadError', { msg: e.response?.data?.detail || e.message }))
  }
})
</script>

<style scoped lang="scss">
.editor-page {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.editor-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  border-bottom: 1px solid var(--app-border);
  background: var(--app-surface);
  flex-wrap: wrap;
  gap: 12px;
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.plan-name {
  font-size: 16px;
  font-weight: 500;
  color: var(--app-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}

.toolbar-center {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  min-width: 0;
  justify-content: flex-end;
}

.fx-label {
  font-family: monospace;
  font-weight: bold;
}

.editor-body {
  display: flex;
  flex: 1;
  overflow: hidden;
  background: var(--app-bg);

  @media (max-width: 768px) {
    flex-direction: column;
    overflow: auto;
  }
}

.tree-panel {
  width: 300px;
  min-width: 220px;
  max-width: 35%;
  border-right: 1px solid var(--app-border);
  background: var(--app-surface);
  display: flex;
  flex-direction: column;
  overflow: hidden;

  @media (max-width: 768px) {
    width: 100%;
    max-width: none;
    border-right: none;
    border-bottom: 1px solid var(--app-border);
  }
}

.panel-header {
  padding: 12px 16px;
  font-weight: 500;
  font-size: 13px;
  color: var(--app-text-secondary);
  border-bottom: 1px solid var(--app-border);
  background: var(--app-surface);
  text-transform: uppercase;
  letter-spacing: 0.05em;
  display: flex;
  align-items: center;
}

:deep(.el-tree) {
  flex: 1;
  overflow-y: auto;
  background: transparent;
  padding: 8px;
  color: var(--app-text);
}

.tree-node {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  min-width: 0;
}

.tree-node.disabled {
  opacity: 0.4;
}

.node-icon {
  font-size: 15px;
}

.node-label {
  flex: 1;
  color: var(--app-text);
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.property-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--app-bg);
}

.property-content {
  flex: 1;
  overflow-y: auto;
  padding: 18px;

  @media (max-width: 768px) {
    padding: 14px;
  }
}

:deep(.el-form-item__label) {
  font-weight: 500;
  color: var(--app-text-secondary);
  font-size: 13px;
}
</style>
