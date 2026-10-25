<template>
  <el-card shadow="never" class="monitoring-panel">
    <template #header>
      <div class="block-header">
        <span>{{ L('服务端监控（Prometheus）', 'Server monitoring (Prometheus)') }}
          <el-tag v-if="dirty" type="warning" size="small" class="dirty">{{ L('未保存', 'Unsaved') }}</el-tag>
        </span>
        <span class="hint">{{ L('压测结束后自动按测试时间窗采集，作为 AI 根因分析的证据', 'Collected for each run window after the test, as evidence for AI root-cause analysis') }}</span>
      </div>
    </template>

    <el-form label-position="top">
      <el-form-item :label="L('Prometheus 地址', 'Prometheus URL')">
        <el-input v-model="draft.prometheus_url" placeholder="http://192.168.1.10:9090" />
      </el-form-item>
    </el-form>

    <div class="presets">
      <span class="hint">{{ L('添加常用指标：', 'Add common metrics:') }}</span>
      <el-button v-for="(label, key) in presetLabels" :key="key" size="small" @click="addPreset(key)">{{ label }}</el-button>
    </div>

    <el-table :data="draft.queries" size="small" :empty-text="L('还没有指标，点上方按钮添加常用指标', 'No metrics yet; add a preset above')">
      <el-table-column :label="L('名称', 'Name')" width="150">
        <template #default="{ row }"><el-input v-model="row.name" size="small" /></template>
      </el-table-column>
      <el-table-column label="PromQL" min-width="320">
        <template #default="{ row }"><el-input v-model="row.promql" size="small" class="mono" /></template>
      </el-table-column>
      <el-table-column :label="L('单位', 'Unit')" width="80">
        <template #default="{ row }"><el-input v-model="row.unit" size="small" /></template>
      </el-table-column>
      <el-table-column :label="L('饱和阈值', 'Saturation')" width="110">
        <template #default="{ row }"><el-input-number v-model="row.saturation" size="small" :controls="false" class="num" /></template>
      </el-table-column>
      <el-table-column :label="L('检测', 'Check')" width="110">
        <template #default="{ $index }">
          <template v-if="testResults[$index]">
            <el-tooltip v-if="testResults[$index].error" :content="testResults[$index].error!">
              <el-tag type="danger" size="small">{{ L('失败', 'Error') }}</el-tag>
            </el-tooltip>
            <el-tag v-else :type="testResults[$index].series ? 'success' : 'warning'" size="small">
              {{ testResults[$index].series ? L(`${testResults[$index].series} 条序列`, `${testResults[$index].series} series`) : L('无数据', 'No data') }}
            </el-tag>
          </template>
        </template>
      </el-table-column>
      <el-table-column width="50">
        <template #default="{ $index }">
          <el-button text type="danger" size="small" @click="draft.queries.splice($index, 1); probe = null"><el-icon><Delete /></el-icon></el-button>
        </template>
      </el-table-column>
    </el-table>
    <el-button text type="primary" class="add" @click="draft.queries.push({ name: '', promql: '', unit: '', saturation: null })">
      <el-icon><Plus /></el-icon> {{ L('自定义指标', 'Custom metric') }}
    </el-button>

    <el-alert v-if="probe" class="probe" :closable="false" show-icon
      :type="!probe.reachable ? 'error' : probeIssues ? 'warning' : 'success'"
      :title="!probe.reachable ? L('连不上 Prometheus', 'Cannot reach Prometheus')
        : L(`已连接 Prometheus ${probe.version || ''}`, `Connected to Prometheus ${probe.version || ''}`)"
      :description="!probe.reachable ? `${probe.error}。${L('检查地址和端口，并确认本机能访问它。', 'Check the address and port, and that this machine can reach it.')}`
        : probeIssues ? L(`${probeIssues} 个指标失败或最近 5 分钟没有数据：查询失败看 PromQL 语法；无数据一般是标签不对或没装对应的 exporter。`,
          `${probeIssues} metrics failed or had no data in the last 5 minutes. A failure means bad PromQL; no data usually means wrong labels or a missing exporter.`) : ''" />

    <div class="actions">
      <el-button :loading="testing" :disabled="!canTest" @click="test">{{ L('测试连接', 'Test connection') }}</el-button>
      <el-button type="primary" :loading="saving" :disabled="!dirty || !canSubmit" @click="save">{{ L('保存', 'Save') }}</el-button>
      <el-button v-if="hasSaved" type="danger" text @click="remove">{{ L('停用监控', 'Disable monitoring') }}</el-button>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, Plus } from '@element-plus/icons-vue'
import { api, type MetricQuery, type MonitoringConfig, type MonitoringTestResult } from '@/api'
import { useI18n } from '@/composables/useI18n'

const props = defineProps<{ projectId: string }>()
const { currentLang } = useI18n()
const L = (zh: string, en: string) => currentLang.value === 'en' ? en : zh

const presetLabels = { node: L('主机 (node_exporter)', 'Host (node_exporter)'), jvm: 'JVM / HikariCP', mysql: 'MySQL' }
const presets = ref<Record<string, MetricQuery[]>>({})
const empty = (): MonitoringConfig => ({ prometheus_url: '', queries: [] })
const draft = ref<MonitoringConfig>(empty())
const savedJson = ref(JSON.stringify(empty()))
const hasSaved = ref(false)
const dirty = computed(() => JSON.stringify(draft.value) !== savedJson.value)
const canSubmit = computed(() => /^https?:\/\/.+/.test(draft.value.prometheus_url.trim())
  && draft.value.queries.length > 0 && draft.value.queries.every(q => q.name.trim() && q.promql.trim()))
const canTest = computed(() => /^https?:\/\/.+/.test(draft.value.prometheus_url.trim()))
const testing = ref(false)
const saving = ref(false)
const probe = ref<MonitoringTestResult | null>(null)
const testResults = computed(() => probe.value?.queries || [])
const probeIssues = computed(() => testResults.value.filter(r => r.error || !r.series).length)

async function load() {
  const id = props.projectId
  try {
    const res = await api.getMonitoring(id)
    if (id !== props.projectId) return
    draft.value = res.data ? { ...empty(), ...res.data } : empty()
    hasSaved.value = !!res.data
    savedJson.value = JSON.stringify(draft.value)
    probe.value = null
  } catch {
    ElMessage.error(L('读取监控配置失败', 'Failed to load monitoring settings'))
  }
}

watch(() => props.projectId, load, { immediate: true })
api.getMonitoringPresets().then(res => { presets.value = res.data }).catch(() => {})

function addPreset(key: string) {
  const existing = new Set(draft.value.queries.map(q => q.promql))
  const added = (presets.value[key] || []).filter(q => !existing.has(q.promql))
  draft.value.queries.push(...added.map(q => ({ ...q })))
  probe.value = null
}

async function test() {
  testing.value = true
  try {
    probe.value = (await api.testMonitoring(props.projectId, draft.value)).data
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail?.[0]?.msg || e.response?.data?.detail || L('测试失败', 'Test failed'))
  } finally {
    testing.value = false
  }
}

async function save() {
  const id = props.projectId
  saving.value = true
  try {
    const res = await api.saveMonitoring(id, draft.value)
    if (id !== props.projectId) return
    draft.value = { ...empty(), ...res.data }
    savedJson.value = JSON.stringify(draft.value)
    hasSaved.value = true
    ElMessage.success(L('已保存，之后的报告会附带服务端指标', 'Saved. Reports will now include server metrics.'))
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail?.[0]?.msg || e.response?.data?.detail || L('保存失败', 'Save failed'))
  } finally {
    saving.value = false
  }
}

async function remove() {
  try {
    await ElMessageBox.confirm(L('停用后报告将不再采集服务端指标，确定吗？', 'Reports will stop collecting server metrics. Continue?'),
      L('停用监控', 'Disable monitoring'), { type: 'warning' })
  } catch { return }
  await api.deleteMonitoring(props.projectId)
  await load()
}
</script>

<style scoped lang="scss">
.block-header { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.hint { color: var(--app-text-muted); font-size: 12px; }
.dirty { margin-left: 8px; }
.presets { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.mono :deep(input) { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
.num { width: 100%; }
.add { margin-top: 8px; }
.probe { margin-top: 16px; }
.actions { margin-top: 16px; display: flex; gap: 8px; flex-wrap: wrap; }
</style>
