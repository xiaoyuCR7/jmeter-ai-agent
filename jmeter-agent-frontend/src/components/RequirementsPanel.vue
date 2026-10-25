<template>
  <div class="requirements-panel">
    <el-card shadow="never" class="block">
      <template #header>
        <div class="block-header">
          <span>{{ L('从测试文档提取', 'Extract from a test document') }}</span>
          <span class="hint">{{ L('AI 只生成草稿，确认保存前不会生效', 'AI drafts only; nothing applies until you save') }}</span>
        </div>
      </template>
      <el-input v-model="docText" type="textarea" :rows="8" resize="vertical"
        :placeholder="L('粘贴需求说明 / 测试方案中关于性能的段落，例如：下单接口 95% 响应时间不超过 1 秒，系统错误率低于 0.1%，支持 500 并发……', 'Paste the performance section of the spec or test plan, e.g. checkout P95 within 1 s, error rate below 0.1%, 500 concurrent users…')" />
      <div class="extract-actions">
        <label class="file-pick">
          <input type="file" accept=".txt,.md,.markdown,.csv,.json" @change="onFile" />
          <el-button text type="primary" @click.prevent="pickFile">{{ L('读取 .txt / .md 文件', 'Load a .txt / .md file') }}</el-button>
        </label>
        <span v-if="sourceName" class="hint">{{ sourceName }}</span>
        <el-select v-model="model" :placeholder="L('选择模型', 'Model')" size="default" class="model-select">
          <el-option v-for="m in models" :key="m.id" :label="m.name || m.id" :value="m.id" />
        </el-select>
        <el-button type="primary" :loading="extracting" :disabled="!docText.trim() || !model" @click="extract">
          {{ L('AI 提取需求', 'Extract with AI') }}
        </el-button>
      </div>
    </el-card>

    <el-card shadow="never" class="block">
      <template #header>
        <div class="block-header">
          <span>{{ L('性能需求', 'Performance requirements') }}
            <el-tag v-if="dirty" type="warning" size="small" class="dirty">{{ L('未保存', 'Unsaved') }}</el-tag>
          </span>
          <span class="hint" v-if="draft.updated_at && !dirty">{{ L('保存于', 'Saved') }} {{ draft.updated_at.replace('T', ' ') }}</span>
        </div>
      </template>

      <el-table :data="draft.items" size="small" class="req-table" :empty-text="L('还没有需求，可从文档提取或手动添加', 'No requirements yet: extract from a document or add one')">
        <el-table-column :label="L('编号', 'ID')" width="80">
          <template #default="{ row }"><el-input v-model="row.id" size="small" /></template>
        </el-table-column>
        <el-table-column :label="L('对象', 'Applies to')" min-width="300">
          <template #default="{ row }">
            <div class="target">
              <el-select v-model="row.scope" size="small" class="scope" @change="row.transaction = row.scope === 'global' ? null : (row.transaction || '')">
                <el-option :label="L('整体', 'Overall')" value="global" />
                <el-option :label="L('接口', 'Transaction')" value="transaction" />
              </el-select>
              <el-input v-if="row.scope === 'transaction'" v-model="row.transaction" size="small"
                :placeholder="L('接口（如 POST /api/order）或采样器名', 'Endpoint (e.g. POST /api/order) or sampler label')" />
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="L('指标', 'Metric')" width="150">
          <template #default="{ row }">
            <el-select v-model="row.metric" size="small">
              <el-option v-for="(label, key) in metricLabels" :key="key" :label="label" :value="key" />
            </el-select>
          </template>
        </el-table-column>
        <el-table-column :label="L('条件', 'Condition')" width="170">
          <template #default="{ row }">
            <div class="condition">
              <el-select v-model="row.operator" size="small" class="op">
                <el-option v-for="(label, key) in operatorLabels" :key="key" :label="label" :value="key" />
              </el-select>
              <el-input-number v-model="row.value" size="small" :controls="false" class="num" />
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="L('文档原文', 'Source quote')" min-width="220">
          <template #default="{ row }">
            <div class="source">
              <el-tooltip v-if="row.source" :content="row.source_verified ? L('原文已在文档中找到', 'Quote found in the document') : L('原文未在文档中找到，请核对', 'Quote not found in the document; please verify')">
                <el-tag :type="row.source_verified ? 'success' : 'warning'" size="small">{{ row.source_verified ? L('已核对', 'Verified') : L('待核对', 'Check') }}</el-tag>
              </el-tooltip>
              <el-input v-model="row.source" size="small" @input="row.source_verified = false" />
            </div>
          </template>
        </el-table-column>
        <el-table-column width="56">
          <template #default="{ $index }">
            <el-button text type="danger" size="small" @click="draft.items.splice($index, 1)"><el-icon><Delete /></el-icon></el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-button text type="primary" class="add" @click="addItem"><el-icon><Plus /></el-icon> {{ L('添加需求', 'Add requirement') }}</el-button>

      <h4>{{ L('负载模型', 'Load profile') }}</h4>
      <div class="profile">
        <label>{{ L('并发用户', 'Concurrent users') }}<el-input-number v-model="draft.load_profile.concurrency" :min="1" :controls="false" /></label>
        <label>{{ L('加压时间(秒)', 'Ramp-up (s)') }}<el-input-number v-model="draft.load_profile.ramp_up_seconds" :min="0" :controls="false" /></label>
        <label>{{ L('持续时间(秒)', 'Duration (s)') }}<el-input-number v-model="draft.load_profile.duration_seconds" :min="1" :controls="false" /></label>
        <label>{{ L('目标 TPS', 'Target TPS') }}<el-input-number v-model="draft.load_profile.target_tps" :min="0" :controls="false" /></label>
      </div>

      <el-alert v-if="draft.assumptions.length" type="info" :closable="false" class="assumptions"
        :title="L('提取时的假设与缺失信息', 'Assumptions and gaps found during extraction')">
        <ul><li v-for="(a, i) in draft.assumptions" :key="i">{{ a }}</li></ul>
      </el-alert>

      <div class="save-actions">
        <el-button type="primary" :loading="saving" :disabled="!dirty" @click="save">{{ L('保存需求', 'Save requirements') }}</el-button>
        <el-button :disabled="!dirty" @click="load">{{ L('撤销修改', 'Discard changes') }}</el-button>
        <el-button v-if="hasSaved" type="danger" text @click="remove">{{ L('清空需求', 'Clear requirements') }}</el-button>
      </div>
    </el-card>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Plus } from '@element-plus/icons-vue'
import { api, type PerformanceRequirements, type RequirementItem } from '@/api'
import { useI18n } from '@/composables/useI18n'

const props = defineProps<{ projectId: string; initialDoc?: string }>()
const { currentLang } = useI18n()
const L = (zh: string, en: string) => currentLang.value === 'en' ? en : zh

const metricLabels = computed<Record<string, string>>(() => ({
  avg_response_time: L('平均响应时间 ms', 'Avg response ms'), p90: 'P90 ms', p95: 'P95 ms', p99: 'P99 ms',
  max_response_time: L('最大响应时间 ms', 'Max response ms'), error_rate: L('错误率 %', 'Error rate %'),
  throughput: L('吞吐量 TPS', 'Throughput TPS'),
}))
const operatorLabels = { lte: '≤', lt: '<', gte: '≥', gt: '>' }

const empty = (): PerformanceRequirements => ({
  items: [], assumptions: [], source_name: '', updated_at: null,
  load_profile: { concurrency: null, ramp_up_seconds: null, duration_seconds: null, target_tps: null, source: '' },
})

const draft = ref<PerformanceRequirements>(empty())
const savedJson = ref(JSON.stringify(empty()))
const hasSaved = ref(false)
const dirty = computed(() => JSON.stringify(draft.value) !== savedJson.value)
const docText = ref(props.initialDoc || '')
const sourceName = ref('')
const models = ref<any[]>([])
const model = ref('')
const extracting = ref(false)
const saving = ref(false)

function normalize(data: PerformanceRequirements | null): PerformanceRequirements {
  const base = empty()
  if (!data) return base
  return { ...base, ...data, load_profile: { ...base.load_profile, ...(data.load_profile || {}) } }
}

async function load() {
  const id = props.projectId
  try {
    const res = await api.getRequirements(id)
    if (id !== props.projectId) return
    draft.value = normalize(res.data)
    hasSaved.value = !!res.data
    savedJson.value = JSON.stringify(draft.value)
  } catch {
    ElMessage.error(L('读取性能需求失败', 'Failed to load requirements'))
  }
}

async function loadModels() {
  try {
    const res = await api.getModels()
    models.value = (res.data || []).filter((m: any) => m.enabled !== false)
    if (!model.value && models.value.length) model.value = models.value[0].id
  } catch { /* the extract button stays disabled */ }
}

watch(() => props.projectId, () => { load() }, { immediate: true })
loadModels()

function pickFile(event: MouseEvent) {
  const label = (event.currentTarget as HTMLElement).closest('label')
  label?.querySelector<HTMLInputElement>('input[type=file]')?.click()
}

async function onFile(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  if (file.size > 2 * 1024 * 1024) {
    ElMessage.error(L('文件超过 2MB，请只粘贴性能相关段落', 'File exceeds 2 MB; paste only the performance section'))
  } else {
    docText.value = await file.text()
    sourceName.value = file.name
  }
  input.value = ''
}

async function extract() {
  if (dirty.value && draft.value.items.length) {
    try {
      await ElMessageBox.confirm(L('提取结果会替换当前未保存的编辑，继续吗？', 'The draft will replace your unsaved edits. Continue?'),
        L('替换草稿', 'Replace draft'), { type: 'warning' })
    } catch { return }
  }
  const id = props.projectId
  extracting.value = true
  try {
    const res = await api.extractRequirements(id, docText.value, sourceName.value, model.value)
    if (id !== props.projectId) return
    draft.value = normalize({ ...res.data, updated_at: draft.value.updated_at })
    const unverified = draft.value.items.filter(i => !i.source_verified).length
    ElMessage.success(L(`提取到 ${draft.value.items.length} 条需求`, `Extracted ${draft.value.items.length} requirements`)
      + (unverified ? L(`，其中 ${unverified} 条原文需核对`, `; ${unverified} quotes need checking`) : ''))
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || L('提取失败', 'Extraction failed'))
  } finally {
    extracting.value = false
  }
}

function addItem() {
  const used = new Set(draft.value.items.map(i => i.id))
  let n = draft.value.items.length + 1
  while (used.has(`R${n}`)) n++
  const item: RequirementItem = { id: `R${n}`, scope: 'global', transaction: null, metric: 'p95', operator: 'lte',
    value: 1000, source: '', source_verified: false }
  draft.value.items.push(item)
}

async function save() {
  const ids = draft.value.items.map(i => i.id.trim())
  if (ids.some(id => !id) || new Set(ids).size !== ids.length) {
    ElMessage.error(L('需求编号不能为空且不能重复', 'Requirement IDs must be unique and non-empty'))
    return
  }
  if (draft.value.items.some(i => i.scope === 'transaction' && !i.transaction?.trim())) {
    ElMessage.error(L('接口级需求必须填写采样器名称', 'Transaction requirements need a sampler label'))
    return
  }
  const id = props.projectId
  saving.value = true
  try {
    const res = await api.saveRequirements(id, draft.value)
    if (id !== props.projectId) return
    draft.value = normalize(res.data)
    savedJson.value = JSON.stringify(draft.value)
    hasSaved.value = true
    ElMessage.success(L('性能需求已保存，之后的报告会逐条判定达标情况', 'Saved. Reports will now judge each requirement.'))
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail?.[0]?.msg || e.response?.data?.detail || L('保存失败', 'Save failed'))
  } finally {
    saving.value = false
  }
}

async function remove() {
  try {
    await ElMessageBox.confirm(L('清空后报告将改用默认阈值，确定吗？', 'Reports will fall back to default thresholds. Clear?'),
      L('清空性能需求', 'Clear requirements'), { type: 'warning' })
  } catch { return }
  await api.deleteRequirements(props.projectId)
  await load()
}
</script>

<style scoped lang="scss">
.requirements-panel { display: flex; flex-direction: column; gap: 16px; }
.block-header { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.hint { color: var(--app-text-muted); font-size: 12px; }
.dirty { margin-left: 8px; }
.extract-actions { display: flex; align-items: center; gap: 12px; margin-top: 12px; flex-wrap: wrap; }
.file-pick input { display: none; }
.model-select { width: 200px; margin-left: auto; }
.req-table :deep(.el-input-number) { width: 100%; }
.target, .condition, .source { display: flex; gap: 6px; align-items: center; }
.scope { width: 88px; flex-shrink: 0; }
.op { width: 64px; flex-shrink: 0; }
.num { flex: 1; min-width: 70px; }
.add { margin-top: 8px; }
h4 { margin: 20px 0 10px; font-size: 14px; }
.profile { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px; }
.profile label { display: flex; flex-direction: column; gap: 6px; font-size: 13px; color: var(--app-text-secondary); }
.profile :deep(.el-input-number) { width: 100%; }
.assumptions { margin-top: 16px; ul { margin: 6px 0 0; padding-left: 18px; } }
.save-actions { margin-top: 20px; display: flex; gap: 8px; flex-wrap: wrap; }
@media (max-width: 768px) { .model-select { margin-left: 0; width: 100%; } }
</style>
