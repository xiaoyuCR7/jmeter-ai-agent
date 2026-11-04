<template>
  <div class="live-run">
    <header class="live-head">
      <div class="title-block">
        <router-link :to="live?.project_id ? `/executions?projectId=${live.project_id}` : '/executions'" class="back">← {{ L('执行记录', 'Runs') }}</router-link>
        <h2>
          {{ live?.plan_name || executionId }}
          <el-tag size="small" :type="live?.status === 'running' ? 'warning' : 'info'" effect="dark" class="state">
            <span class="pulse" v-if="live?.status === 'running'" />{{ stateText }}
          </el-tag>
          <el-tag v-if="purposeText" size="small" type="info">{{ purposeText }}</el-tag>
        </h2>
        <div class="meta num">
          <span>{{ L('已运行', 'Elapsed') }} {{ clock(live?.elapsed_seconds) }}</span>
          <span v-if="live?.planned_seconds">{{ L('计划', 'Planned') }} {{ clock(live.planned_seconds) }}</span>
          <span v-if="live?.remaining_seconds != null">{{ L('剩余约', 'About') }} {{ clock(live.remaining_seconds) }}{{ L('', ' left') }}</span>
          <span v-else-if="live?.available">{{ L('按循环次数结束，无法预估剩余时间', 'Ends by loop count; no time estimate') }}</span>
        </div>
      </div>
      <el-button type="danger" plain :loading="stopping" :disabled="!stoppable" @click="stop">{{ L('停止运行', 'Stop run') }}</el-button>
    </header>

    <el-progress v-if="live?.planned_seconds" :percentage="live.progress" :stroke-width="6" :show-text="false" class="bar" />

    <div v-if="!live?.available || !points.length" class="waiting">
      <el-icon class="is-loading"><Loading /></el-icon>
      {{ live?.status === 'pending' ? L('排队等待开始…', 'Waiting to start…') : L('JMeter 正在启动，等待第一批结果…', 'JMeter is starting; waiting for the first results…') }}
    </div>

    <template v-else>
      <section class="kpis">
        <div v-for="kpi in kpis" :key="kpi.label" class="kpi">
          <div class="kpi-label">{{ kpi.label }}</div>
          <div :class="['kpi-value', 'num', kpi.tone]">{{ kpi.value }}</div>
          <div class="kpi-sub num">{{ kpi.sub }}</div>
        </div>
      </section>

      <section class="panel">
        <div class="panel-head">
          <h3>{{ L('实时趋势', 'Live trend') }}</h3>
          <span class="hint">{{ L('每 5 秒一个点；最新一段还在统计中，不画出', 'One point per 5 s; the newest, still filling, is left out') }}</span>
        </div>
        <v-chart class="chart" :option="loadOption" autoresize />
        <v-chart class="chart" :option="latencyOption" autoresize />
      </section>

      <section class="panel">
        <div class="panel-head">
          <h3>{{ L('先看这些接口', 'Requests to look at first') }}</h3>
          <span class="hint">{{ L('错误最多的在前，其次是最慢的', 'Most errors first, then the slowest') }}</span>
        </div>
        <el-table :data="live.labels" size="small">
          <el-table-column :label="L('接口', 'Request')" prop="label" min-width="200" show-overflow-tooltip />
          <el-table-column :label="L('请求数', 'Requests')" prop="requests" width="100" align="right" />
          <el-table-column :label="L('错误率', 'Errors')" width="110" align="right">
            <template #default="{ row }"><span :class="['num', { bad: row.errors }]">{{ row.error_rate }}%</span></template>
          </el-table-column>
          <el-table-column :label="L('平均 (ms)', 'Avg (ms)')" prop="avg" width="110" align="right" />
        </el-table>
      </section>

      <p v-if="live.guard.enabled" class="hint guard">
        {{ L(`运行守护已开启：最近一段时间的请求错误率连续两次达到 ${live.guard.error_rate}% 时，会自动停止这次运行。`,
             `The run guard is on: the run stops by itself when recent requests fail at ${live.guard.error_rate}% or more twice in a row.`) }}
      </p>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Loading } from '@element-plus/icons-vue'
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, LegendComponent } from 'echarts/components'
import VChart from 'vue-echarts'
import { api, type LiveRun } from '@/api'
import { useI18n } from '@/composables/useI18n'

use([CanvasRenderer, LineChart, GridComponent, TooltipComponent, LegendComponent])

const props = defineProps<{ executionId: string }>()
const emit = defineEmits<{ ended: [status: string] }>()
const { L } = useI18n()

const POLL_MS = 3000
const live = ref<LiveRun | null>(null)
const stopping = ref(false)
let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false

// The newest bucket is still filling; drawing it would show a false drop at the end of every line.
const points = computed(() => (live.value?.series || []).filter(point => !point.partial))
const stoppable = computed(() => live.value?.status === 'running' || live.value?.status === 'pending')
const stateText = computed(() => ({ pending: L('排队中', 'Queued'), running: L('运行中', 'Running') } as Record<string, string>)[live.value?.status || ''] || live.value?.status || '')
const purposeText = computed(() => {
  const purpose = live.value?.purpose
  if (purpose?.kind === 'capacity') return L(`容量探索 · 第 ${(purpose.step ?? 0) + 1} 级`, `Capacity search · step ${(purpose.step ?? 0) + 1}`)
  if (purpose?.kind === 'verification') return L('验证运行', 'Verification run')
  return ''
})

function clock(seconds?: number | null) {
  if (seconds == null) return '—'
  const s = Math.max(0, Math.round(seconds))
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(r).padStart(2, '0')}` : `${m}:${String(r).padStart(2, '0')}`
}

const kpis = computed(() => {
  const w = live.value?.window, total = live.value?.totals
  if (!w || !total) return []
  const guard = live.value!.guard.error_rate
  const errorTone = w.error_rate >= guard / 2 ? 'bad' : w.error_rate > 1 ? 'warn' : ''
  const planned = live.value!.planned_threads
  return [
    { label: L('当前吞吐量', 'Throughput now'), value: `${w.throughput}/s`, sub: L(`全程平均 ${total.throughput}/s`, `${total.throughput}/s overall`), tone: '' },
    { label: L('当前 P95', 'P95 now'), value: `${w.p95} ms`, sub: L(`当前平均 ${w.avg} ms`, `avg ${w.avg} ms now`), tone: '' },
    { label: L('当前错误率', 'Errors now'), value: `${w.error_rate}%`, sub: L(`全程 ${total.error_rate}%`, `${total.error_rate}% overall`), tone: errorTone },
    { label: L('活跃线程', 'Active threads'), value: String(total.threads), sub: planned ? L(`计划 ${planned}`, `${planned} planned`) : '', tone: '' },
    { label: L('累计请求', 'Requests'), value: total.requests.toLocaleString(), sub: L(`失败 ${total.errors.toLocaleString()}`, `${total.errors.toLocaleString()} failed`), tone: '' },
  ]
})

const cssVar = (name: string) => getComputedStyle(document.documentElement).getPropertyValue(name).trim()
const axisStyle = () => ({
  axisLine: { lineStyle: { color: cssVar('--app-border-strong') } },
  axisLabel: { color: cssVar('--app-text-muted') },
  splitLine: { lineStyle: { color: cssVar('--app-border') } },
})
const base = (names: string[]) => ({
  animation: false,
  tooltip: { trigger: 'axis' },
  legend: { top: 0, data: names, textStyle: { color: cssVar('--app-text-secondary') } },
  grid: { left: 56, right: 56, top: 36, bottom: 28 },
  xAxis: { type: 'category', data: points.value.map(point => clock(point.t)), ...axisStyle() },
})
const line = (name: string, data: number[], color: string, yAxisIndex = 0) =>
  ({ name, type: 'line', showSymbol: false, data, color, yAxisIndex })

const loadOption = computed(() => {
  const tps = L('吞吐量 (/s)', 'Throughput (/s)'), errors = L('错误率 (%)', 'Errors (%)'), threads = L('线程', 'Threads')
  return {
    ...base([tps, errors, threads]),
    yAxis: [{ type: 'value', name: '/s · %', ...axisStyle() }, { type: 'value', name: threads, ...axisStyle(), splitLine: { show: false } }],
    series: [
      line(tps, points.value.map(p => p.throughput), cssVar('--app-primary')),
      line(errors, points.value.map(p => p.error_rate), cssVar('--app-danger')),
      line(threads, points.value.map(p => p.threads), cssVar('--app-text-muted'), 1),
    ],
  }
})

const latencyOption = computed(() => {
  const avg = L('平均响应时间', 'Avg response time')
  return {
    ...base([avg, 'P95']),
    yAxis: { type: 'value', name: 'ms', ...axisStyle() },
    series: [
      line(avg, points.value.map(p => p.avg), cssVar('--app-primary')),
      line('P95', points.value.map(p => p.p95), cssVar('--app-warning')),
    ],
  }
})

async function poll() {
  clearTimeout(timer)
  try {
    live.value = (await api.getExecutionLive(props.executionId)).data
  } catch {
    // A missed poll is retried on the next tick.
  }
  if (disposed) return
  if (live.value && !['pending', 'running'].includes(live.value.status)) {
    emit('ended', live.value.status)
    return
  }
  timer = setTimeout(poll, POLL_MS)
}

async function stop() {
  try {
    await ElMessageBox.confirm(L('停止后 JMeter 会结束这次运行，已有的结果会保留并生成报告。', 'JMeter will end this run; the results so far are kept and reported.'),
      L('停止运行？', 'Stop the run?'), { type: 'warning', confirmButtonText: L('停止', 'Stop'), cancelButtonText: L('继续运行', 'Keep running') })
  } catch {
    return
  }
  stopping.value = true
  try {
    const { data } = await api.cancelExecution(props.executionId)
    if (!data.success) ElMessage.warning(data.message || L('运行已经结束', 'The run already ended'))
    await poll()
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || L('停止失败', 'Could not stop the run'))
  } finally {
    stopping.value = false
  }
}

watch(() => props.executionId, () => { live.value = null; poll() }, { immediate: true })
onUnmounted(() => { disposed = true; clearTimeout(timer) })
</script>

<style scoped lang="scss">
.live-run { display: flex; flex-direction: column; gap: 16px; }
.live-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; flex-wrap: wrap; }
.back { font-size: 13px; color: var(--app-text-muted); text-decoration: none; }
h2 { margin: 6px 0 4px; font-size: 20px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.state { display: inline-flex; align-items: center; gap: 6px; }
.pulse {
  width: 7px; height: 7px; border-radius: 50%; background: currentColor; display: inline-block; margin-right: 4px;
  animation: pulse 1.4s ease-in-out infinite;
}
@keyframes pulse { 50% { opacity: 0.25; } }
@media (prefers-reduced-motion: reduce) { .pulse { animation: none; } }
.meta { display: flex; gap: 16px; flex-wrap: wrap; font-size: 13px; color: var(--app-text-secondary); }
.waiting { display: flex; align-items: center; gap: 8px; padding: 32px; justify-content: center; color: var(--app-text-secondary);
  border: 1px dashed var(--app-border); border-radius: var(--app-radius-lg); }
.kpis {
  display: grid; grid-template-columns: repeat(5, minmax(0, 1fr));
  border: 1px solid var(--app-border); border-radius: var(--app-radius-lg); background: var(--app-surface); overflow: hidden;
  @media (max-width: 900px) { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .kpi { padding: 12px 16px; border-right: 1px solid var(--app-border); &:last-child { border-right: none; } }
  .kpi-label { font-size: 12px; color: var(--app-text-muted); }
  .kpi-value { font-size: 24px; font-weight: 600; line-height: 1.3; color: var(--app-text); margin-top: 2px; font-variant-numeric: tabular-nums; }
  .kpi-value.bad { color: var(--app-danger); }
  .kpi-value.warn { color: var(--app-warning); }
  .kpi-sub { margin-top: 2px; font-size: 12px; color: var(--app-text-muted); white-space: nowrap; }
}
.chart { height: 240px; }
.num { font-variant-numeric: tabular-nums; }
.bad { color: var(--app-danger); }
.guard { margin: 0; }
</style>
