<template>
  <div class="evidence-panel">
    <section class="panel">
    <div class="section-header">
      <h4>{{ L('性能需求达标', 'Requirements') }}</h4>
      <div class="header-actions">
        <el-button text size="small" @click="$router.push(`/projects/${projectId}/requirements`)">{{ L('编辑需求', 'Edit requirements') }}</el-button>
        <el-button text size="small" :loading="rejudging" @click="rejudge">{{ L('按当前需求重新判定', 'Re-check against current requirements') }}</el-button>
      </div>
    </div>
    <template v-if="req && req.verdict !== 'none'">
      <p class="tally">
        {{ L('通过', 'Passed') }} <b class="ok">{{ req.passed }}</b> ·
        {{ L('不通过', 'Failed') }} <b class="bad">{{ req.failed }}</b> ·
        {{ L('无法评估', 'No data') }} <b class="muted">{{ req.missing }}</b>
      </p>
      <el-table :data="req.items" size="small" :row-class-name="rowClass">
        <el-table-column :label="L('编号', 'ID')" prop="id" width="70" />
        <el-table-column :label="L('需求', 'Requirement')" min-width="220">
          <template #default="{ row }">
            <div>{{ describe(row) }}</div>
            <div v-if="row.matched_labels.length > 1" class="sub">{{ L('匹配接口', 'Matched') }}: {{ row.matched_labels.join(', ') }}</div>
          </template>
        </el-table-column>
        <el-table-column :label="L('实际值', 'Actual')" width="120" align="right">
          <template #default="{ row }">
            <span v-if="row.actual === null" class="muted">{{ L('无数据', 'n/a') }}</span>
            <span v-else>{{ format(row.actual) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="L('结果', 'Result')" width="130">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ statusText(row.status) }}</el-tag>
            <span v-if="row.gap_pct !== null" class="gap">+{{ row.gap_pct }}%</span>
          </template>
        </el-table-column>
        <el-table-column :label="L('文档原文', 'Source')" min-width="220">
          <template #default="{ row }">
            <span class="quote" :class="{ unverified: row.source && !row.source_verified }">{{ row.source || '—' }}</span>
          </template>
        </el-table-column>
      </el-table>
    </template>

    <div v-else class="empty">
      <p>{{ L('本项目还没有录入性能需求，下面按默认阈值评估。', 'This project has no performance requirements yet; default thresholds are used below.') }}</p>
      <ul v-if="slaResult" class="sla">
        <li v-for="(v, i) in slaResult.violations" :key="i" class="bad">{{ v }}</li>
        <li v-if="!slaResult.violations?.length" class="ok">{{ L('默认阈值全部通过', 'All default thresholds passed') }}</li>
      </ul>
      <el-button type="primary" text @click="$router.push(`/projects/${projectId}/requirements`)">
        {{ L('从测试文档录入需求 →', 'Add requirements from a test document →') }}
      </el-button>
    </div>

    </section>

    <section class="panel">
      <div class="section-header">
        <h4>{{ L('历史对比', 'History') }}</h4>
        <el-tag v-if="history?.is_baseline" type="success" size="small">{{ L('本次即当前基线', 'This run is the baseline') }}</el-tag>
        <el-button v-else text size="small" :loading="promoting" @click="promote">{{ L('设为基线', 'Set as baseline') }}</el-button>
      </div>
      <p v-if="!history?.baseline && !history?.previous" class="muted">
        {{ L('还没有可对比的基线或同一测试计划的历史执行。', 'No baseline or earlier run of this test plan to compare with.') }}
      </p>
      <div v-for="c in comparisons" :key="c.kind" class="comparison">
        <div class="comparison-title">
          <span>{{ c.kind === 'baseline' ? L('对比基线', 'vs baseline') : L('对比上一次', 'vs previous run') }}「{{ c.data.name }}」</span>
          <el-tag :type="historyType(c.data.verdict)" size="small">{{ historyText(c.data.verdict) }}</el-tag>
        </div>
        <div class="deltas">
          <span v-for="d in c.data.deltas.filter(d => d.metric !== 'min_response_time')" :key="d.metric" class="delta" :class="d.is_regression ? 'bad' : d.direction === 'better' ? 'ok' : ''">
            {{ metricTitle(d.metric) }} {{ d.baseline }} → {{ d.target }}
            <small v-if="d.delta_pct !== null">({{ d.delta_pct > 0 ? '+' : '' }}{{ d.delta_pct }}%)</small>
          </span>
        </div>
        <el-table v-if="c.data.transactions.length" :data="c.data.transactions" size="small" class="tx-table">
          <el-table-column :label="L('接口', 'Transaction')" prop="label" min-width="200" />
          <el-table-column :label="L('指标', 'Metric')" prop="metric_title" width="90" />
          <el-table-column :label="L('变化', 'Change')" min-width="170">
            <template #default="{ row }">
              <span :class="row.is_regression ? 'bad' : 'ok'">{{ row.baseline }} → {{ row.target }}
                <small v-if="row.delta_pct !== null">({{ row.delta_pct > 0 ? '+' : '' }}{{ row.delta_pct }}%)</small></span>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </section>

    <section class="panel">
      <div class="section-header">
        <h4>{{ L('服务端指标', 'Server metrics') }}</h4>
        <el-button v-if="server && server.status !== 'not_configured'" text size="small" :loading="refreshing" @click="refresh">
          {{ L('重新采集', 'Re-collect') }}
        </el-button>
      </div>
      <div v-if="!server || server.status === 'not_configured'" class="empty">
        <p>{{ L('未配置 Prometheus，AI 只能根据客户端数据推测根因。', 'Prometheus is not configured, so AI can only guess root causes from client-side data.') }}</p>
        <el-button type="primary" text @click="$router.push(`/projects/${projectId}/settings?section=monitoring`)">
          {{ L('配置服务端监控 →', 'Configure server monitoring →') }}
        </el-button>
      </div>
      <el-alert v-else-if="server.status === 'error'" type="error" :closable="false" :title="L('采集失败：', 'Collection failed: ') + (server.error || '')"
        :description="L('多数是 Prometheus 地址不通或 PromQL 有误，可以到项目设置里点「测试连接」排查。', 'Usually the Prometheus address is unreachable or a PromQL query is wrong. Use Test connection in project settings to check.')" />
      <el-alert v-else-if="server.status === 'no_data'" type="warning" :closable="false" :title="L('Prometheus 连得上，但压测期间没有数据', 'Prometheus answered, but had no data for this run')"
        :description="L('常见原因：查询的标签和实际不符、目标机器没装对应的 exporter，或这次执行早于 Prometheus 的数据保留期。', 'Common causes: the query labels do not match, the target has no matching exporter, or the run is older than Prometheus retention.')" />
      <template v-else>
        <ul v-if="server.findings?.length" class="findings">
          <li v-for="(f, i) in server.findings" :key="i">{{ f }}</li>
        </ul>
        <el-table :data="server.metrics" size="small">
          <el-table-column :label="L('指标', 'Metric')" min-width="170">
            <template #default="{ row }">
              <div>{{ row.name }}</div>
              <div v-if="row.series && row.series !== 'all'" class="sub">{{ row.series }}</div>
            </template>
          </el-table-column>
          <el-table-column :label="L('平均 / P95 / 峰值', 'Avg / P95 / Peak')" min-width="190">
            <template #default="{ row }">
              <span v-if="row.error" class="bad">{{ row.error }}</span>
              <span v-else-if="row.no_data" class="muted">{{ L('压测期间无数据', 'No data during the run') }}</span>
              <span v-else>{{ row.avg }} / {{ row.p95 }} / <b>{{ row.max }}</b>{{ row.unit }} <span class="sub">@{{ row.peak_at_s }}s</span></span>
            </template>
          </el-table-column>
          <el-table-column :label="L('饱和时间', 'Saturated')" width="110" align="right">
            <template #default="{ row }">
              <span v-if="row.above_saturation_pct == null" class="muted">—</span>
              <span v-else :class="{ bad: row.above_saturation_pct > 0 }">{{ row.above_saturation_pct }}%</span>
            </template>
          </el-table-column>
          <el-table-column :label="L('与响应时间相关', 'Corr. with RT')" width="130" align="right">
            <template #default="{ row }">
              <span v-if="row.corr_response_time == null" class="muted">—</span>
              <span v-else :class="{ bad: row.corr_response_time >= 0.7 }">{{ row.corr_response_time }}</span>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="server.chart?.length" class="chart-block">
          <el-select v-model="chartIndex" size="small" class="chart-select">
            <el-option v-for="(c, i) in server.chart" :key="i" :label="c.series !== 'all' ? `${c.name} · ${c.series}` : c.name" :value="i" />
          </el-select>
          <v-chart class="chart" :option="chartOption" autoresize />
        </div>
      </template>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import { api, type ExecutionEvidence } from '@/api'
import { useI18n } from '@/composables/useI18n'

use([CanvasRenderer, LineChart, GridComponent, TooltipComponent, LegendComponent])

const props = defineProps<{
  evidence: ExecutionEvidence | null
  slaResult?: any
  projectId: string
  timeline?: { time_range: string; avg_response_time: number }[]
}>()
const emit = defineEmits<{ updated: [evidence: ExecutionEvidence] }>()
const { currentLang } = useI18n()
const L = (zh: string, en: string) => currentLang.value === 'en' ? en : zh

const req = computed(() => props.evidence?.requirements ?? null)

const statusType = (s: string) => ({ pass: 'success', fail: 'danger', missing: 'info' } as Record<string, any>)[s]
const statusText = (s: string) => ({ pass: L('达标', 'Pass'), fail: L('不达标', 'Fail'), missing: L('无数据', 'No data') } as Record<string, string>)[s]
const rowClass = ({ row }: { row: { status: string } }) => row.status === 'fail' ? 'row-fail' : ''
const format = (n: number) => Number.isInteger(n) ? String(n) : n.toFixed(2)
// Built here rather than from the server's description, which is written for the (Chinese) analysis prompt.
const describe = (row: { transaction: string | null; metric: string; operator: string; expected: number }) => {
  const metric = ({
    avg_response_time: L('平均响应时间 ms', 'Avg response ms'), p90: 'P90 ms', p95: 'P95 ms', p99: 'P99 ms',
    max_response_time: L('最大响应时间 ms', 'Max response ms'), error_rate: L('错误率 %', 'Error rate %'),
    throughput: L('吞吐量 TPS', 'Throughput TPS'),
  } as Record<string, string>)[row.metric] || row.metric
  const op = ({ lte: '≤', lt: '<', gte: '≥', gt: '>' } as Record<string, string>)[row.operator] || row.operator
  return `${row.transaction ? `[${row.transaction}] ` : ''}${metric} ${op} ${row.expected}`
}

const history = computed(() => props.evidence?.baseline ?? null)
const comparisons = computed(() => [
  ...(history.value?.baseline ? [{ kind: 'baseline', data: history.value.baseline }] : []),
  ...(history.value?.previous ? [{ kind: 'previous', data: history.value.previous }] : []),
])
const historyType = (v: string) => ({ regression: 'danger', mixed: 'warning', improvement: 'success', stable: 'info', unknown: 'info' } as Record<string, any>)[v]
const historyText = (v: string) => ({
  regression: L('性能退化', 'Regression'), mixed: L('有升有降', 'Mixed'), improvement: L('性能提升', 'Improved'),
  stable: L('基本持平', 'Stable'), unknown: L('无法比较', 'Unknown'),
} as Record<string, string>)[v]
const metricTitle = (m: string) => ({
  avg_response_time: L('平均', 'Avg'), p95_response_time: 'P95', p99_response_time: 'P99',
  max_response_time: L('最大', 'Max'), error_rate: L('错误率', 'Errors %'), throughput: 'TPS',
} as Record<string, string>)[m] || m
const promoting = ref(false)
const rejudging = ref(false)

// Evidence is recomputed on every request, so fetching it again applies requirement edits made since the page loaded.
async function rejudge() {
  if (!props.evidence) return
  rejudging.value = true
  try {
    emit('updated', (await api.getExecutionEvidence(props.evidence.execution_id)).data)
    ElMessage.success(L('已按当前需求重新判定', 'Re-checked against the current requirements'))
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || L('重新判定失败', 'Re-check failed'))
  } finally {
    rejudging.value = false
  }
}

async function promote() {
  if (!props.evidence) return
  promoting.value = true
  try {
    await api.promoteBaseline(props.evidence.execution_id)
    emit('updated', (await api.getExecutionEvidence(props.evidence.execution_id)).data)
    ElMessage.success(L('已设为该测试计划的基线', 'Set as the baseline for this test plan'))
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || L('设置基线失败', 'Could not set baseline'))
  } finally {
    promoting.value = false
  }
}

const server = computed(() => props.evidence?.server_metrics ?? null)
const chartIndex = ref(0)
watch(server, () => { chartIndex.value = 0 })
const refreshing = ref(false)

async function refresh() {
  if (!props.evidence) return
  refreshing.value = true
  try {
    emit('updated', (await api.refreshServerMetrics(props.evidence.execution_id)).data)
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || L('重新采集失败', 'Re-collection failed'))
  } finally {
    refreshing.value = false
  }
}

// Plot the chosen server metric against JMeter's 30 s response-time segments on one time axis.
const chartOption = computed(() => {
  const series = server.value?.chart?.[chartIndex.value]
  if (!series) return {}
  const rt = (props.timeline || []).map(s => [Number(s.time_range.split('-')[0]) + 15, s.avg_response_time])
  return {
    tooltip: { trigger: 'axis' },
    legend: { data: [series.name, L('平均响应时间', 'Avg response time')] },
    grid: { left: 50, right: 50, top: 36, bottom: 30 },
    xAxis: { type: 'value', name: 's', min: 'dataMin', max: 'dataMax' },
    yAxis: [{ type: 'value', name: series.unit }, { type: 'value', name: 'ms', splitLine: { show: false } }],
    series: [
      { name: series.name, type: 'line', showSymbol: false, data: series.points },
      { name: L('平均响应时间', 'Avg response time'), type: 'line', yAxisIndex: 1, data: rt },
    ],
  }
})
</script>

<style scoped lang="scss">
.evidence-panel { display: flex; flex-direction: column; gap: 16px; }
.tally { margin: 0 0 12px; color: var(--app-text-secondary); font-variant-numeric: tabular-nums; }
.ok { color: var(--app-success); }
.bad { color: var(--app-danger); }
.muted { color: var(--app-text-muted); }
.sub { font-size: 12px; color: var(--app-text-muted); margin-top: 2px; }
.gap { margin-left: 6px; font-size: 12px; color: var(--app-danger); font-variant-numeric: tabular-nums; }
.quote { font-size: 12px; color: var(--app-text-secondary); }
.quote.unverified { color: var(--app-warning); }
.empty p { margin: 0 0 8px; color: var(--app-text-secondary); }
.sla { margin: 0 0 8px; padding-left: 18px; font-size: 13px; }
.comparison { margin-bottom: 14px; }
.comparison-title { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; font-size: 13px; color: var(--app-text); }
.deltas { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 13px; font-variant-numeric: tabular-nums; color: var(--app-text-secondary); }
.delta.bad { color: var(--app-danger); }
.delta.ok { color: var(--app-success); }
.delta small { color: inherit; opacity: .8; }
.tx-table { margin-top: 8px; }
.section-header { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-bottom: 10px; }
.header-actions { display: flex; gap: 4px; flex-wrap: wrap; }
.section-header h4 { margin: 0; font-size: 15px; font-weight: 600; color: var(--app-text); }
.findings { margin: 0 0 12px; padding-left: 18px; color: var(--app-danger); font-size: 13px; }
.chart-block { margin-top: 12px; }
.chart-select { width: 320px; max-width: 100%; }
.chart { height: 280px; margin-top: 8px; }
:deep(.row-fail) td { background: var(--app-danger-soft) !important; }
</style>
