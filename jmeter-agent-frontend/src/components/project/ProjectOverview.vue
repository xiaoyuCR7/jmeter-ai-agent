<template>
  <div class="overview" v-loading="loading">
    <section class="panel">
      <div class="panel-head">
        <div>
          <h3>{{ L('闭环进度', 'Loop setup') }}</h3>
          <p class="hint">{{ L('四步都配好后，每份报告都会给出需求判定、历史对比和服务端证据', 'With all four in place, every report carries verdicts, history comparison and server evidence') }}</p>
        </div>
      </div>
      <ol class="steps">
        <li v-for="(step, i) in steps" :key="step.key" :class="{ done: step.done }">
          <div class="step-top">
            <span class="step-index">
              <el-icon v-if="step.done"><Check /></el-icon>
              <template v-else>{{ i + 1 }}</template>
            </span>
            <span class="step-title">{{ step.title }}</span>
          </div>
          <p class="step-status">{{ step.status }}</p>
          <el-button size="small" :type="step.done ? 'default' : 'primary'" :plain="!step.done" @click="router.push(step.to)">
            {{ step.done ? L('查看', 'View') : step.action }}
          </el-button>
        </li>
      </ol>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h3>{{ L('最近执行', 'Recent runs') }}</h3>
        <router-link :to="`/executions?projectId=${projectId}`" class="more">{{ L('全部执行记录', 'All runs') }} →</router-link>
      </div>
      <el-table :data="recent" :empty-text="L('还没有执行记录', 'No runs yet')" class="clickable"
        @row-click="(row: any) => row.status === 'completed' && router.push(`/executions/${row.id}`)">
        <el-table-column :label="L('开始时间', 'Started')" width="170">
          <template #default="{ row }"><span class="num">{{ formatTime(row.start_time) }}</span></template>
        </el-table-column>
        <el-table-column :label="L('测试计划', 'Test plan')" prop="test_plan_name" min-width="160" show-overflow-tooltip />
        <el-table-column :label="L('状态', 'Status')" width="124">
          <template #default="{ row }"><StatusTag :status="row.status" /></template>
        </el-table-column>
        <el-table-column :label="L('需求判定', 'Verdict')" width="130">
          <template #default="{ row }"><VerdictTag v-if="row.metrics" :verdict="row.requirements" /></template>
        </el-table-column>
        <el-table-column label="P95" width="100" align="right">
          <template #default="{ row }"><span class="num">{{ formatMs(row.metrics?.p95_response_time) }}</span></template>
        </el-table-column>
        <el-table-column :label="L('错误率', 'Errors')" width="90" align="right">
          <template #default="{ row }"><span class="num">{{ formatPct(row.metrics?.error_rate) }}</span></template>
        </el-table-column>
      </el-table>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Check } from '@element-plus/icons-vue'
import { api, type MonitoringConfig, type PerformanceRequirements } from '@/api'
import { useI18n } from '@/composables/useI18n'
import { formatMs, formatPct, formatTime } from '@/utils/format'
import StatusTag from '@/components/StatusTag.vue'
import VerdictTag from '@/components/VerdictTag.vue'

const props = defineProps<{ projectId: string }>()
const router = useRouter()
const { L } = useI18n()

const loading = ref(false)
const requirements = ref<PerformanceRequirements | null>(null)
const planCount = ref(0)
const monitoring = ref<MonitoringConfig | null>(null)
const baselines = ref<any[]>([])
const recent = ref<any[]>([])

async function load() {
  const id = props.projectId
  loading.value = true
  const [reqs, plans, mon, bases, runs] = await Promise.allSettled([
    api.getRequirements(id), api.getProjectTestPlans(id), api.getMonitoring(id), api.getBaselines(id), api.getExecutions(id),
  ])
  if (id !== props.projectId) return
  requirements.value = reqs.status === 'fulfilled' ? reqs.value.data : null
  planCount.value = plans.status === 'fulfilled' ? (plans.value.data || []).length : 0
  monitoring.value = mon.status === 'fulfilled' ? mon.value.data : null
  baselines.value = bases.status === 'fulfilled' ? bases.value.data || [] : []
  recent.value = runs.status === 'fulfilled' ? (runs.value.data?.executions || []).slice(0, 5) : []
  loading.value = false
}
watch(() => props.projectId, load, { immediate: true })

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`

const steps = computed(() => {
  const base = `/projects/${props.projectId}`
  const reqCount = requirements.value?.items.length || 0
  const active = baselines.value.filter(b => b.is_active)
  const queries = monitoring.value?.queries.length || 0
  return [
    { key: 'requirements', title: L('性能需求', 'Performance requirements'), done: reqCount > 0, to: `${base}/requirements`,
      action: L('从文档提取', 'Extract from document'),
      status: reqCount ? L(`${reqCount} 条需求`, plural(reqCount, 'requirement')) : L('还没有需求，报告无法给出达标判定', 'None yet, so reports cannot give verdicts') },
    { key: 'plans', title: L('测试计划', 'Test plans'), done: planCount.value > 0, to: `${base}/plans`,
      action: L('新建测试计划', 'New test plan'),
      status: planCount.value ? L(`${planCount.value} 个测试计划`, plural(planCount.value, 'test plan')) : L('还没有测试计划', 'No test plans yet') },
    { key: 'monitoring', title: L('服务端监控', 'Server monitoring'), done: queries > 0, to: `${base}/settings?section=monitoring`,
      action: L('配置 Prometheus', 'Configure Prometheus'),
      status: queries ? L(`${queries} 个指标`, plural(queries, 'metric')) : L('可选：配置后报告会附带服务端指标', 'Optional: adds server metrics to reports') },
    { key: 'baselines', title: L('性能基线', 'Baseline'), done: active.length > 0, to: `${base}/baselines`,
      action: L('查看说明', 'How to add'),
      status: active.length ? active.map(b => b.name).join('、') : L('在一份满意的报告上点「设为基线」', 'Use "Set as baseline" on a good report') },
  ]
})
</script>

<style scoped lang="scss">
.overview { display: flex; flex-direction: column; gap: 16px; }

.steps {
  list-style: none;
  margin: 0;
  padding: 0;
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;

  @media (max-width: 1200px) { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  @media (max-width: 560px) { grid-template-columns: 1fr; }

  li {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 6px;
    padding: 12px 14px;
    border: 1px solid var(--app-border);
    border-radius: var(--app-radius-lg);
    background: var(--app-surface);

    &.done { background: var(--app-surface-muted); }
    &.done .step-index { background: var(--app-success-soft); color: var(--app-success); border-color: transparent; }
    .el-button { margin-top: auto; }
  }

  .step-top { display: flex; align-items: center; gap: 8px; }

  .step-index {
    width: 26px;
    height: 26px;
    flex-shrink: 0;
    border-radius: 50%;
    border: 1px solid var(--app-border-strong);
    color: var(--app-text-secondary);
    display: grid;
    place-items: center;
    font-size: 13px;
    font-weight: 600;
  }
  .step-title { font-weight: 600; color: var(--app-text); }
  .step-status { margin: 0 0 4px; font-size: 12px; line-height: 1.5; color: var(--app-text-muted); min-height: 36px; }
}

.more { font-size: 13px; color: var(--app-primary); text-decoration: none; }
.clickable :deep(.el-table__row) { cursor: pointer; }
</style>
