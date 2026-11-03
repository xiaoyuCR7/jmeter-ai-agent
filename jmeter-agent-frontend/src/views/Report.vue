<template>
  <div class="report-page" v-loading="loading">
    <template v-if="report">
      <header class="report-head">
        <div class="title-block">
          <router-link :to="`/executions?projectId=${report.project_id}`" class="back">← {{ L('执行记录', 'Runs') }}</router-link>
          <h2>{{ report.test_plan_name }}</h2>
          <div class="meta num">
            <span>{{ formatTime(report.start_time) }}</span>
            <span>{{ L('时长', 'Duration') }} {{ formatDuration(report.start_time, report.end_time) }}</span>
            <span>ID {{ report.execution_id }}</span>
            <router-link :to="`/projects/${report.project_id}`">{{ L('所属项目', 'Project') }}</router-link>
          </div>
          <p v-if="report.environment" class="run-env">
            <el-tag size="small" :type="report.environment.kind === 'production' ? 'danger' : 'info'">
              {{ L('环境', 'Environment') }}：{{ report.environment.name }}
            </el-tag>
            <span v-if="report.environment.base_url">{{ report.environment.base_url }}</span>
            <span v-if="report.environment.note" class="env-note">{{ report.environment.note }}</span>
          </p>
        </div>
        <div class="head-actions button-row">
          <el-button v-if="report.raw_data_path" @click="downloadJTL">{{ t('report.downloadJtl') }}</el-button>
          <el-button v-if="report.html_report_path" @click="openHtmlReport">{{ t('report.viewHtml') }}</el-button>
          <el-button v-if="report.ai_analysis" @click="downloadAnalysis">{{ L('下载 AI 分析', 'Download AI analysis') }}</el-button>
        </div>
      </header>

      <section :class="['verdict-banner', banner.tone]">
        <div class="verdict-main">
          <span class="verdict-label">{{ banner.title }}</span>
          <span class="verdict-detail">{{ banner.detail }}</span>
        </div>
        <div v-if="historyLines.length" class="verdict-history">
          <span v-for="line in historyLines" :key="line.text" :class="line.tone">{{ line.text }}</span>
        </div>
      </section>

      <section v-if="failure" class="panel failure">
        <h3>{{ failure.title }}</h3>
        <p>{{ failure.advice }}</p>
        <code v-if="report.failure_diagnosis.line">{{ report.failure_diagnosis.line }}</code>
      </section>

      <section class="kpi-block">
        <div v-if="reference" class="kpi-caption">
          <span>{{ reference.kind === 'baseline' ? L('对比基线', 'Compared with baseline') : L('对比上一次执行', 'Compared with the previous run') }}「{{ reference.data.name }}」</span>
          <span class="hint">{{ L(`变化超过 ${reference.data.threshold_pct ?? 20}% 且变差记为退化`, `A change worse than ${reference.data.threshold_pct ?? 20}% counts as a regression`) }}</span>
        </div>
        <div class="kpis">
          <div v-for="kpi in kpis" :key="kpi.label" class="kpi">
            <div class="kpi-label">{{ kpi.label }}</div>
            <div :class="['kpi-value', 'num', kpi.tone]">{{ kpi.value }}</div>
            <div v-if="kpi.delta" :class="['kpi-delta', 'num', kpi.delta.tone]" :title="kpi.delta.title">
              {{ kpi.delta.text }} <span class="was">{{ kpi.delta.was }}</span>
            </div>
          </div>
        </div>
      </section>

      <el-tabs v-model="activeTab" class="report-tabs">
        <el-tab-pane :label="L('判定与证据', 'Verdict & evidence')" name="evidence">
          <EvidencePanel :evidence="report.evidence" :sla-result="report.sla_result" :project-id="report.project_id"
            :timeline="report.summary?.timeline_segments" @updated="report.evidence = $event" />
        </el-tab-pane>

        <el-tab-pane :label="L('趋势', 'Trends')" name="trends" lazy>
          <section class="panel">
            <div class="panel-head"><h3>{{ L('响应时间与吞吐量', 'Response time and throughput') }}</h3></div>
            <v-chart v-if="timeline.length" class="chart" :option="trendOption" autoresize />
            <p v-else class="hint">{{ L('本次执行没有时间段数据', 'No time-segment data for this run') }}</p>
          </section>
          <section class="panel">
            <div class="panel-head"><h3>{{ L('分位数', 'Percentiles') }}</h3></div>
            <div class="percentiles">
              <v-chart class="chart small" :option="percentileOption" autoresize />
              <dl class="facts num">
                <template v-for="item in percentileFacts" :key="item.label">
                  <dt>{{ item.label }}</dt><dd>{{ item.value }}</dd>
                </template>
              </dl>
            </div>
          </section>
          <section v-if="timeline.length" class="panel">
            <div class="panel-head"><h3>{{ L('分段数据', 'Segments') }}</h3></div>
            <el-table :data="timeline" size="small" max-height="360">
              <el-table-column prop="time_range" :label="t('report.timeSeriesTable.timeRange')" width="110" />
              <el-table-column :label="t('report.timeSeriesTable.requests')" align="right"><template #default="{ row }"><span class="num">{{ row.request_count }}</span></template></el-table-column>
              <el-table-column :label="L('线程', 'Threads')" align="right"><template #default="{ row }"><span class="num">{{ row.active_threads ?? '-' }}</span></template></el-table-column>
              <el-table-column label="Avg" align="right"><template #default="{ row }"><span class="num">{{ formatMs(row.avg_response_time) }}</span></template></el-table-column>
              <el-table-column label="Max" align="right"><template #default="{ row }"><span class="num">{{ formatMs(row.max_response_time) }}</span></template></el-table-column>
              <el-table-column label="TPS" align="right"><template #default="{ row }"><span class="num">{{ formatTps(row.throughput) }}</span></template></el-table-column>
              <el-table-column :label="L('错误率', 'Errors')" align="right">
                <template #default="{ row }"><span :class="['num', { bad: row.error_rate > 0 }]">{{ formatPct(row.error_rate) }}</span></template>
              </el-table-column>
            </el-table>
          </section>
        </el-tab-pane>

        <el-tab-pane :label="L(`接口明细 ${transactions.length}`, `Transactions ${transactions.length}`)" name="transactions" lazy>
          <section class="panel">
            <el-table :data="transactions" size="small" :default-sort="{ prop: 'p95_response_time', order: 'descending' }"
              :empty-text="L('没有接口级数据', 'No per-transaction data')">
              <el-table-column prop="label" :label="L('接口', 'Transaction')" min-width="220" sortable show-overflow-tooltip />
              <el-table-column prop="count" :label="L('样本数', 'Samples')" width="100" align="right" sortable><template #default="{ row }"><span class="num">{{ row.count }}</span></template></el-table-column>
              <el-table-column prop="avg_response_time" label="Avg" width="96" align="right" sortable><template #default="{ row }"><span class="num">{{ formatMs(row.avg_response_time) }}</span></template></el-table-column>
              <el-table-column prop="p90_response_time" label="P90" width="96" align="right" sortable><template #default="{ row }"><span class="num">{{ formatMs(row.p90_response_time) }}</span></template></el-table-column>
              <el-table-column prop="p95_response_time" label="P95" width="96" align="right" sortable><template #default="{ row }"><span class="num">{{ formatMs(row.p95_response_time) }}</span></template></el-table-column>
              <el-table-column prop="p99_response_time" label="P99" width="96" align="right" sortable><template #default="{ row }"><span class="num">{{ formatMs(row.p99_response_time) }}</span></template></el-table-column>
              <el-table-column prop="max_response_time" label="Max" width="96" align="right" sortable><template #default="{ row }"><span class="num">{{ formatMs(row.max_response_time) }}</span></template></el-table-column>
              <el-table-column prop="error_rate" :label="L('错误率', 'Errors')" width="96" align="right" sortable>
                <template #default="{ row }"><span :class="['num', { bad: row.error_rate > 0 }]">{{ formatPct(row.error_rate) }}</span></template>
              </el-table-column>
              <el-table-column prop="throughput" label="TPS" width="84" align="right" sortable><template #default="{ row }"><span class="num">{{ formatTps(row.throughput) }}</span></template></el-table-column>
            </el-table>
          </section>
        </el-tab-pane>

        <el-tab-pane :label="L(`错误 ${errors.length}`, `Errors ${errors.length}`)" name="errors" lazy>
          <section class="panel">
            <el-table :data="errors" size="small" :empty-text="L('没有错误', 'No errors')">
              <el-table-column prop="error_type" :label="t('report.errorType')" min-width="200" show-overflow-tooltip />
              <el-table-column :label="L('接口', 'Transactions')" min-width="180" show-overflow-tooltip>
                <template #default="{ row }">{{ errorLabels(row) }}</template>
              </el-table-column>
              <el-table-column :label="t('report.errorCount')" width="90" align="right"><template #default="{ row }"><span class="num">{{ row.count }}</span></template></el-table-column>
              <el-table-column :label="t('report.errorRatio')" width="90" align="right"><template #default="{ row }"><span class="num">{{ formatPct(row.percentage) }}</span></template></el-table-column>
              <el-table-column :label="t('report.errorExample')" min-width="260" show-overflow-tooltip>
                <template #default="{ row }"><span class="mono">{{ row.sample_messages?.[0] || '-' }}</span></template>
              </el-table-column>
            </el-table>
          </section>
        </el-tab-pane>

        <el-tab-pane :label="L('AI 分析', 'AI analysis')" name="ai" lazy>
          <section v-if="analysisRunning" class="panel analysis-state">
            <el-alert type="info" :closable="false" show-icon
              :title="L('AI 分析正在生成', 'The AI analysis is being written')"
              :description="L('通常需要几十秒，完成后这里会自动显示。', 'This usually takes under a minute; it will show here when done.')" />
          </section>
          <section v-else-if="ai" class="panel ai-analysis">
            <div v-if="ai.analysis_status !== 'completed' && canReanalyze" class="button-row">
              <el-button type="primary" :loading="reanalyzing" @click="reanalyze">{{ L('重新分析', 'Analyse again') }}</el-button>
            </div>
            <el-alert v-if="staleVerdict" type="error" :closable="false" show-icon
              :title="L('需求判定已变化，这份分析可能过时', 'The requirement verdict has changed since this analysis was written')"
              :description="L(`生成分析时：${staleVerdict.then}；现在：${staleVerdict.now}。`, `When written: ${staleVerdict.then}. Now: ${staleVerdict.now}.`)" />
            <el-alert :title="factCheck.title" :description="factCheck.detail" :type="factCheck.type" :closable="false" show-icon />
            <p v-if="usageText(ai.usage, currentLang)" class="ai-usage">{{ L('写这份分析：', 'Writing this analysis: ') }}{{ usageText(ai.usage, currentLang) }}</p>
            <div class="ai-block lead">
              <h4>{{ t('report.evaluation') }}</h4>
              <p>{{ ai.overall_assessment }}</p>
            </div>
            <div class="ai-grid">
              <div v-for="block in aiLists" :key="block.title" class="ai-block">
                <h4>{{ block.title }}</h4>
                <ul>
                  <li v-for="(item, i) in block.items" :key="i">
                    {{ item }}
                    <el-button class="keep" text size="small" @click="openLesson(item)">{{ L('存为经验', 'Save as lesson') }}</el-button>
                  </li>
                </ul>
              </div>
            </div>
            <div v-if="ai.timeline_analysis" class="ai-block">
              <h4>{{ t('report.timelineAnalysis') }}</h4>
              <p>{{ ai.timeline_analysis }}</p>
            </div>
            <div v-if="ai.improvement_plan?.length" class="ai-block">
              <h4>{{ t('report.improvementPlan') }}</h4>
              <ol><li v-for="(item, i) in ai.improvement_plan" :key="i">{{ item }}</li></ol>
            </div>
            <div v-if="ai.conclusion" class="ai-block">
              <h4>{{ t('report.conclusion') }}</h4>
              <p>{{ ai.conclusion }}</p>
              <el-button class="keep" text size="small" @click="openLesson(ai.conclusion)">{{ L('存为经验', 'Save as lesson') }}</el-button>
            </div>
          </section>
          <section v-else class="panel analysis-state">
            <el-alert v-if="report.report_status === 'failed' && report.report_error" type="error" :closable="false" show-icon
              :title="L('AI 分析没有完成', 'The AI analysis did not finish')" :description="report.report_error" />
            <el-empty :description="missingAnalysis">
              <el-button v-if="canReanalyze" type="primary" :loading="reanalyzing" @click="reanalyze">
                {{ L('生成 AI 分析', 'Write the AI analysis') }}
              </el-button>
            </el-empty>
          </section>
          <RunQuestions v-if="ai?.analysis_status === 'completed'" :execution-id="report.execution_id" :follow-ups="ai.follow_ups" />
        </el-tab-pane>

        <el-tab-pane :label="L('经验与假设', 'Lessons')" name="memory" lazy>
          <RunMemory :execution-id="report.execution_id" :refresh-key="lessonsSaved" @save-lesson="openLesson()" />
        </el-tab-pane>
      </el-tabs>
      <SaveLessonDialog ref="lessonDialog" :execution-id="report.execution_id" :labels="transactions.map(row => row.label)"
        @saved="lessonsSaved++" />
    </template>

    <el-empty v-else-if="!loading" :description="t('report.notFound')" />
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { BarChart, LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import { api } from '@/api'
import EvidencePanel from '@/components/EvidencePanel.vue'
import RunQuestions from '@/components/RunQuestions.vue'
import RunMemory from '@/components/RunMemory.vue'
import SaveLessonDialog from '@/components/SaveLessonDialog.vue'
import { useI18n } from '@/composables/useI18n'
import { formatDuration, formatMs, formatPct, formatTime, formatTps, usageText } from '@/utils/format'

use([CanvasRenderer, BarChart, LineChart, GridComponent, TooltipComponent, LegendComponent])

const { t, currentLang } = useI18n()
const L = (zh: string, en: string) => currentLang.value === 'en' ? en : zh
const route = useRoute()

const loading = ref(true)
const report = ref<any>(null)
const activeTab = ref('evidence')

const summary = computed(() => report.value?.summary || {})
const timeline = computed<any[]>(() => summary.value.timeline_segments || [])
const errors = computed<any[]>(() => summary.value.error_details || [])
const transactions = computed(() => Object.entries(summary.value.per_label || {}).map(([label, stats]: [string, any]) => ({ label, ...stats })))
const ai = computed(() => report.value?.ai_analysis || null)
const analysisRunning = computed(() => report.value?.report_status === 'running')
const canReanalyze = computed(() => report.value?.status === 'completed' && summary.value.total_requests > 0)
const missingAnalysis = computed(() => {
  if (report.value?.status !== 'completed') return L('运行没有成功完成，不生成 AI 分析。', 'Runs that did not complete get no AI analysis.')
  if (!summary.value.total_requests) return L('这次运行没有请求结果，无法分析。', 'This run has no request results to analyse.')
  if (report.value?.analysis_enabled === false) return L('自动 AI 分析已关闭，可以手动生成。', 'Automatic AI analysis is off; you can write one now.')
  return L('这次运行还没有 AI 分析。', 'This run has no AI analysis yet.')
})
const reanalyzing = ref(false)
const lessonDialog = ref<InstanceType<typeof SaveLessonDialog>>()
const lessonsSaved = ref(0)
// A finding names requests the way the report does, so those requests are preselected.
const openLesson = (content = '') =>
  lessonDialog.value?.open(content, transactions.value.map(row => row.label).filter(label => content.includes(label)))
const POLL_MS = 4000
let pollTimer: ReturnType<typeof setTimeout> | undefined
let disposed = false
const errorLabels = (row: any) => Object.entries(row.labels || {}).map(([label, count]) => `${label} (${count})`).join(', ') || '-'

const banner = computed(() => {
  const req = report.value?.evidence?.requirements
  if (req && req.verdict !== 'none') {
    const detail = L(`达标 ${req.passed} · 不达标 ${req.failed} · 无数据 ${req.missing}`, `${req.passed} met · ${req.failed} not met · ${req.missing} no data`)
    if (req.verdict === 'pass') return { tone: 'pass', title: L('性能需求全部达标', 'All requirements met'), detail }
    if (req.verdict === 'fail') return { tone: 'fail', title: L('性能需求未达标', 'Requirements not met'), detail }
    if (req.load_gaps?.length && !req.missing) return { tone: 'incomplete', title: L('负载未达到需求，不能算达标', 'Below the required load, so this is not a pass'), detail }
    return { tone: 'incomplete', title: L('部分需求无法评估', 'Some requirements could not be evaluated'), detail }
  }
  const violations = report.value?.sla_result?.violations?.length || 0
  return {
    tone: violations ? 'fail' : 'neutral',
    title: L('未设性能需求', 'No performance requirements'),
    detail: violations ? L(`按默认阈值有 ${violations} 项超标`, `${violations} default thresholds exceeded`) : L('按默认阈值评估全部通过', 'All default thresholds passed'),
  }
})

const failure = computed(() => {
  const code = report.value?.failure_diagnosis?.code
  if (!code) return null
  const known: Record<string, { title: string; advice: string }> = {
    load_generator_busy: { title: L('压测机太忙，这次没有开始', 'The machine was too busy to start this run'), advice: L('关掉占用 CPU 或内存的程序后重新运行。', 'Close what is using CPU or memory, then run it again.') },
    timed_out: { title: L('运行超过了时间上限', 'The run went past the time limit'), advice: L('计划的时长可能超过了 JMETER_TIMEOUT，缩短计划或调大这个设置。', 'The plan may run longer than JMETER_TIMEOUT. Shorten the plan or raise that setting.') },
    java_missing: { title: L('找不到 Java', 'Java was not found'), advice: L('JMeter 需要 Java。安装 Java 并设置 JAVA_HOME 后再运行。', 'JMeter needs Java. Install it, set JAVA_HOME, then run again.') },
    out_of_memory: { title: L('JMeter 内存不足', 'JMeter ran out of memory'), advice: L('调大 JMeter 的堆内存，或减少并发和保存的结果字段。', 'Give JMeter a larger heap, or reduce the load or the result fields saved.') },
    missing_plugin: { title: L('计划用到了没有安装的插件', 'The plan uses a plugin that is not installed'), advice: L('把下面这行提到的插件装进 JMeter 的 lib/ext，或从计划里去掉这个组件。', 'Add the plugin named below to JMeter\'s lib/ext, or remove that element from the plan.') },
    missing_file: { title: L('计划引用的文件找不到', 'A file the plan needs is missing'), advice: L('把文件放到计划所在目录，或在项目的测试数据里重新上传。', 'Put the file next to the plan, or upload it again under test data.') },
    port_in_use: { title: L('需要的端口被占用', 'A port the run needs is in use'), advice: L('关掉占用端口的程序，或等上一次运行结束后再试。', 'Close what is using the port, or wait for the previous run to finish.') },
    remote_engine: { title: L('连不上远程压测引擎', 'Could not reach a remote engine'), advice: L('确认远程引擎已启动，地址和 RMI 端口正确。', 'Check the remote engines are running and their addresses and RMI ports are right.') },
    plan_unreadable: { title: L('JMeter 读不了这个计划', 'JMeter could not read this plan'), advice: L('在计划编辑器里检查计划，或查看执行日志。', 'Check the plan in the editor, or read the run log.') },
    unknown: { title: L('运行没有产生结果', 'The run produced no results'), advice: L('下面是日志里最后一条错误；完整内容在执行记录的日志里。', 'Below is the last error in the log. The full log is in the run list.') },
  }
  return known[code] || known.unknown
})

const historyLines = computed(() => {
  const history = report.value?.evidence?.baseline
  const text: Record<string, string> = {
    regression: L('性能退化', 'regression'), mixed: L('有升有降', 'mixed'), improvement: L('性能提升', 'improved'),
    stable: L('基本持平', 'stable'), unknown: L('无法比较', 'not comparable'),
  }
  const tone = (v: string) => (v === 'regression' ? 'bad' : v === 'improvement' ? 'good' : '')
  return [
    ...(history?.baseline ? [{ text: `${L('对比基线', 'vs baseline')}：${text[history.baseline.verdict]}`, tone: tone(history.baseline.verdict) }] : []),
    ...(history?.previous ? [{ text: `${L('对比上一次', 'vs previous')}：${text[history.previous.verdict]}`, tone: tone(history.previous.verdict) }] : []),
  ]
})

// The baseline wins over the previous run when both exist; it is the reference the team agreed on.
const reference = computed(() => {
  const history = report.value?.evidence?.baseline
  if (history?.baseline?.deltas?.length) return { kind: 'baseline', data: history.baseline }
  if (history?.previous?.deltas?.length) return { kind: 'previous', data: history.previous }
  return null
})

function deltaFor(metric: string, format: (v: number) => string) {
  const d = reference.value?.data.deltas.find((x: any) => x.metric === metric)
  if (!d) return null
  const arrow = d.target > d.baseline ? '▲' : d.target < d.baseline ? '▼' : '='
  const change = d.delta_pct === null ? '' : ` ${d.delta_pct > 0 ? '+' : ''}${d.delta_pct}%`
  return {
    text: `${arrow}${change}`,
    was: `${L('原', 'was')} ${format(d.baseline)}`,
    tone: d.is_regression ? 'bad' : d.direction === 'better' ? 'good' : '',
    title: d.is_regression ? L('退化', 'Regression') : d.direction === 'better' ? L('变好', 'Better') : L('基本持平', 'Stable'),
  }
}

const kpis = computed(() => {
  const s = summary.value
  return [
    { label: L('总请求', 'Requests'), value: (s.total_requests ?? 0).toLocaleString(), delta: null },
    { label: L('平均响应', 'Avg'), value: formatMs(s.avg_response_time), delta: deltaFor('avg_response_time', formatMs) },
    { label: 'P95', value: formatMs(s.percentiles?.p95 ?? s.p95_response_time), delta: deltaFor('p95_response_time', formatMs) },
    { label: 'P99', value: formatMs(s.percentiles?.p99 ?? s.p99_response_time), delta: deltaFor('p99_response_time', formatMs) },
    { label: L('错误率', 'Error rate'), value: formatPct(s.error_rate), tone: s.error_rate > 0 ? 'bad' : '', delta: deltaFor('error_rate', formatPct) },
    { label: L('吞吐量 TPS', 'Throughput'), value: formatTps(s.throughput), delta: deltaFor('throughput', formatTps) },
  ]
})

const formatBytes = (bytes?: number) => {
  if (!bytes) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB']
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)))
  return `${parseFloat((bytes / 1024 ** i).toFixed(2))} ${units[i]}`
}

const percentileFacts = computed(() => {
  const s = summary.value
  const p = s.percentiles || {}
  return [
    { label: 'Min', value: formatMs(s.min_response_time) },
    { label: 'P50', value: formatMs(p.p50) },
    { label: 'P90', value: formatMs(p.p90) },
    { label: 'P95', value: formatMs(p.p95) },
    { label: 'P99', value: formatMs(p.p99) },
    { label: 'Max', value: formatMs(s.max_response_time) },
    { label: L('接收', 'Received'), value: `${formatBytes(s.received_bytes_per_sec)}/s` },
    { label: L('发送', 'Sent'), value: `${formatBytes(s.sent_bytes_per_sec)}/s` },
  ]
})

// Chart colors follow the CSS tokens so both themes stay legible.
const cssVar = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim()
const axisStyle = () => ({
  axisLine: { lineStyle: { color: cssVar('--app-border-strong') } },
  axisLabel: { color: cssVar('--app-text-muted') },
  splitLine: { lineStyle: { color: cssVar('--app-border') } },
})

const trendOption = computed(() => ({
  tooltip: { trigger: 'axis' },
  legend: { top: 0, data: [L('平均响应时间', 'Avg response time'), 'TPS', L('错误率', 'Error rate')], textStyle: { color: cssVar('--app-text-secondary') } },
  grid: { left: 56, right: 56, top: 48, bottom: 32 },
  xAxis: { type: 'category', data: timeline.value.map(s => s.time_range), ...axisStyle() },
  yAxis: [
    { type: 'value', name: 'ms', ...axisStyle() },
    { type: 'value', name: 'TPS / %', ...axisStyle(), splitLine: { show: false } },
  ],
  series: [
    { name: L('平均响应时间', 'Avg response time'), type: 'line', showSymbol: false, data: timeline.value.map(s => s.avg_response_time), color: cssVar('--app-primary') },
    { name: 'TPS', type: 'line', yAxisIndex: 1, showSymbol: false, data: timeline.value.map(s => s.throughput), color: cssVar('--app-text-muted') },
    { name: L('错误率', 'Error rate'), type: 'line', yAxisIndex: 1, showSymbol: false, data: timeline.value.map(s => s.error_rate), color: cssVar('--app-danger') },
  ],
}))

const percentileOption = computed(() => {
  const p = summary.value.percentiles || {}
  return {
    tooltip: { trigger: 'axis' },
    grid: { left: 56, right: 16, top: 24, bottom: 28 },
    xAxis: { type: 'category', data: ['P50', 'P90', 'P95', 'P99'], ...axisStyle() },
    yAxis: { type: 'value', name: 'ms', ...axisStyle() },
    series: [{ type: 'bar', barWidth: '40%', data: [p.p50, p.p90, p.p95, p.p99], color: cssVar('--app-primary'),
      label: { show: true, position: 'top', color: cssVar('--app-text-secondary'), formatter: '{c}' } }],
  }
})

// The analysis stores the verdict it saw; flag it once requirements (or a re-check) move the verdict.
const staleVerdict = computed(() => {
  const then = ai.value?.requirements_verdict
  const now = report.value?.evidence?.requirements
  if (!then || !now) return null
  const same = ['verdict', 'passed', 'failed', 'missing'].every(k => then[k] === now[k])
  const describe = (v: any) => v.verdict === 'none'
    ? L('未设性能需求', 'no requirements')
    : L(`达标 ${v.passed} · 不达标 ${v.failed} · 无数据 ${v.missing}`, `${v.passed} met · ${v.failed} not met · ${v.missing} no data`)
  return same ? null : { then: describe(then), now: describe(now) }
})

const factCheck = computed(() => {
  const a = ai.value
  const fixed = a?.fact_check?.corrected_from
  const fixedNote = fixed ? L(`初稿有 ${fixed} 处数字与实测不符，已让模型按核对结果改写。`, `The first draft had ${fixed} numbers that did not match; the model rewrote them.`) : ''
  if (a?.evidence_status === 'mismatch') {
    const wrong = (a.fact_check?.mismatches || []).map((m: any) => `${m.metric}: ${m.claimed} → ${m.actual}`).join('；')
    return { type: 'error' as const, title: L('AI 文本里有数字与实测不符，请以指标为准', 'Some numbers in the AI text do not match the measurements; trust the metrics'), detail: fixedNote + wrong }
  }
  if (a?.evidence_status === 'verified') {
    const count = a.fact_check?.checked ?? 0
    return { type: 'info' as const, title: L(`已核对 ${count} 处指标数字，均与实测相符`, `${count} metric values checked against the measurements`), detail: fixedNote + L('这只核对数字；原因和建议仍是推测，不等于已验证的根因。', 'Only the numbers are checked. Causes and advice are still hypotheses, not verified root causes.') }
  }
  return { type: 'warning' as const, title: t('report.evidenceWarning'), detail: '' }
})

const aiLists = computed(() => {
  const a = ai.value
  if (!a) return []
  return [
    { title: t('report.keyFindings'), items: a.key_findings },
    { title: t('report.issueLocation'), items: a.specific_issues },
    { title: t('report.bottleneck'), items: a.performance_bottlenecks },
    { title: t('report.resourceBottleneck'), items: a.resource_bottlenecks },
    { title: t('report.errorRootCause'), items: a.error_analysis },
    { title: t('report.optimization'), items: a.recommendations },
    { title: t('report.riskAreas'), items: a.risk_areas },
    { title: L('分析限制', 'Limits of this analysis'), items: a.limitations },
  ].filter(block => block.items?.length)
})

async function loadReport(quiet = false) {
  const id = route.params.id as string
  if (!quiet) loading.value = true
  try {
    report.value = (await api.getReport(id)).data
  } catch {
    if (!quiet) report.value = null
  } finally {
    loading.value = false
  }
  // Keep checking while the analysis is written, so it appears without a manual refresh.
  clearTimeout(pollTimer)
  if (!disposed && analysisRunning.value) pollTimer = setTimeout(() => loadReport(true), POLL_MS)
}

async function reanalyze() {
  reanalyzing.value = true
  try {
    await api.reanalyzeRun(report.value.execution_id)
    await loadReport(true)
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || L('无法开始分析', 'Could not start the analysis'))
  } finally {
    reanalyzing.value = false
  }
}

function downloadJTL() {
  const a = document.createElement('a')
  a.href = `/reports/${report.value.execution_id}/result.jtl`
  a.download = `result_${report.value.execution_id}.jtl`
  a.click()
}

function openHtmlReport() {
  window.open(`/reports/${report.value.execution_id}/html/index.html`, '_blank', 'noopener')
}

async function downloadAnalysis() {
  try {
    const res = await api.downloadAnalysisReport(report.value.execution_id)
    const url = URL.createObjectURL(new Blob([res.data], { type: 'text/markdown' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `ai_analysis_${report.value.execution_id}.md`
    a.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch {
    ElMessage.error(L('AI 分析的 Markdown 文件不存在', 'The AI analysis Markdown file is not available'))
  }
}

onMounted(() => loadReport())
onUnmounted(() => {
  disposed = true
  clearTimeout(pollTimer)
})
</script>

<style scoped lang="scss">
.ai-usage { margin: 0; font-size: 12px; color: var(--app-text-muted); }
.report-page { max-width: 1440px; margin: 0 auto; display: flex; flex-direction: column; gap: 16px; min-height: 200px; }

.report-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  gap: 16px;
  flex-wrap: wrap;

  .back { font-size: 13px; color: var(--app-text-muted); text-decoration: none; &:hover { color: var(--app-primary); } }
  h2 { margin: 2px 0; font-size: 20px; font-weight: 600; color: var(--app-text); }
  .meta {
    display: flex; gap: 14px; flex-wrap: wrap; font-size: 13px; color: var(--app-text-muted);
    a { color: var(--app-primary); text-decoration: none; }
  }
  .run-env {
    display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin: 6px 0 0;
    font-size: 13px; color: var(--app-text-muted); overflow-wrap: anywhere;
    .env-note { color: var(--app-warning, #b26b00); }
  }
  .head-actions { display: flex; gap: 8px; flex-wrap: wrap; }
}

.verdict-banner {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px 24px;
  flex-wrap: wrap;
  padding: 14px 18px;
  border-radius: var(--app-radius-lg);
  border: 1px solid var(--app-border);
  background: var(--app-surface);

  &.pass { border-color: transparent; background: var(--app-success-soft); .verdict-label { color: var(--app-success); } }
  &.fail { border-color: transparent; background: var(--app-danger-soft); .verdict-label { color: var(--app-danger); } }
  &.incomplete { border-color: transparent; background: var(--app-warning-soft); .verdict-label { color: var(--app-warning); } }

  .verdict-main { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  .verdict-label { font-size: 17px; font-weight: 600; color: var(--app-text); }
  .verdict-detail { color: var(--app-text-secondary); font-variant-numeric: tabular-nums; }
  .verdict-history { display: flex; gap: 16px; font-size: 13px; color: var(--app-text-secondary); }
  .bad { color: var(--app-danger); }
  .good { color: var(--app-success); }
}

.failure {
  h3 { margin: 0 0 4px; font-size: 15px; font-weight: 600; color: var(--app-danger); }
  p { margin: 0 0 8px; color: var(--app-text-secondary); }
  code { display: block; font-family: var(--app-font-mono); font-size: 12px; color: var(--app-text-muted); word-break: break-all; }
}

.kpi-block {
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-lg);
  background: var(--app-surface);
  overflow: hidden;
}

.kpi-caption {
  display: flex;
  justify-content: space-between;
  gap: 8px 16px;
  flex-wrap: wrap;
  padding: 8px 16px;
  border-bottom: 1px solid var(--app-border);
  font-size: 13px;
  color: var(--app-text-secondary);
}

.kpis {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));

  @media (max-width: 900px) { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  @media (max-width: 520px) { grid-template-columns: repeat(2, minmax(0, 1fr)); }

  .kpi { padding: 12px 16px; border-right: 1px solid var(--app-border); &:last-child { border-right: none; } }
  .kpi-label { font-size: 12px; color: var(--app-text-muted); }
  .kpi-value { font-size: 24px; font-weight: 600; line-height: 1.3; letter-spacing: -0.01em; color: var(--app-text); margin-top: 2px; font-variant-numeric: tabular-nums; }
  .kpi-value.bad { color: var(--app-danger); }
  .kpi-delta { margin-top: 2px; font-size: 12px; color: var(--app-text-muted); white-space: nowrap; }
  .kpi-delta.bad { color: var(--app-danger); font-weight: 600; }
  .kpi-delta.good { color: var(--app-success); }
  .kpi-delta .was { font-weight: 400; color: var(--app-text-muted); }
}

.report-tabs :deep(.el-tab-pane) { display: flex; flex-direction: column; gap: 16px; }

.chart { height: 320px; }
.chart.small { height: 240px; }
.percentiles { display: grid; grid-template-columns: minmax(0, 1fr) 240px; gap: 24px; align-items: center;
  @media (max-width: 768px) { grid-template-columns: 1fr; }
}
.facts {
  display: grid; grid-template-columns: auto 1fr; gap: 6px 16px; margin: 0; font-size: 13px;
  dt { color: var(--app-text-muted); }
  dd { margin: 0; text-align: right; color: var(--app-text); }
}
.bad { color: var(--app-danger); }
.mono { font-family: var(--app-font-mono); font-size: 12px; }

.ai-analysis, .analysis-state { display: flex; flex-direction: column; gap: 16px; }
.ai-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; }
.ai-block {
  h4 { margin: 0 0 6px; font-size: 14px; font-weight: 600; color: var(--app-text); }
  p, li { color: var(--app-text-secondary); line-height: 1.7; }
  ul, ol { margin: 0; padding-left: 20px; }
  &.lead p { color: var(--app-text); }
  // Quiet until the reader is on that item, but always reachable by keyboard.
  .keep { height: auto; margin-left: 4px; padding: 0 4px; font-size: 12px; opacity: .6; }
  li:hover .keep, p:hover + .keep, .keep:hover, .keep:focus-visible { opacity: 1; }
}
</style>
