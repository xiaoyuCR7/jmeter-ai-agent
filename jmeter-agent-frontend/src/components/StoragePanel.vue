<template>
  <el-card class="storage">
    <template #header>
      <div class="card-header">
        <span>{{ L('报告占用的空间', 'Space used by reports') }}</span>
        <el-button text size="small" :loading="loading" @click="load">{{ L('刷新', 'Refresh') }}</el-button>
      </div>
    </template>
    <p class="hint">
      {{ L('每次运行都会留下 JMeter 的 HTML 报告（约 3 MB，大部分是相同的模板）和原始结果。清理只删除这两样；运行的摘要、AI 报告、日志和历史都保留，作为当前基线来源的运行不会被清理。',
           'Every run leaves JMeter\'s HTML dashboard (about 3 MB, most of it the same template) and its raw results. Pruning removes only those two; each run\'s summary, AI report, logs and history stay, and the run an active baseline came from is never pruned.') }}
    </p>
    <div v-if="usage" class="figures">
      <div><span class="label">{{ L('运行数', 'Runs') }}</span><b class="num">{{ usage.runs }}</b></div>
      <div><span class="label">{{ L('总占用', 'Total') }}</span><b class="num">{{ size(usage.total_bytes) }}</b></div>
      <div><span class="label">{{ L('HTML 报告', 'HTML dashboards') }}</span><b class="num">{{ size(usage.dashboard_bytes) }}</b></div>
      <div><span class="label">{{ L('原始结果', 'Raw results') }}</span><b class="num">{{ size(usage.result_bytes) }}</b></div>
    </div>
    <p v-if="usage" class="hint">
      {{ usage.retention_days > 0
        ? L(`已开启自动清理：每天清理 ${usage.retention_days} 天前的运行（REPORTS_RETENTION_DAYS）。`, `Automatic pruning is on: runs older than ${usage.retention_days} days are pruned daily (REPORTS_RETENTION_DAYS).`)
        : L('自动清理未开启。在 .env 里设置 REPORTS_RETENTION_DAYS 可以每天自动清理。', 'Automatic pruning is off. Set REPORTS_RETENTION_DAYS in .env to prune daily.') }}
    </p>
    <div class="prune">
      <span>{{ L('清理', 'Prune runs older than') }}</span>
      <el-input-number v-model="days" :min="1" :max="3650" size="small" controls-position="right" />
      <span>{{ L('天前的运行', 'days') }}</span>
      <el-checkbox v-model="includeResults">{{ L('同时删除原始结果（result.jtl）', 'Also remove raw results (result.jtl)') }}</el-checkbox>
      <el-button type="primary" plain size="small" :loading="pruning" @click="prune">{{ L('清理', 'Prune') }}</el-button>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type StorageUsage } from '@/api'
import { useI18n } from '@/composables/useI18n'

const { L } = useI18n()
const usage = ref<StorageUsage | null>(null)
const loading = ref(false)
const pruning = ref(false)
const days = ref(30)
const includeResults = ref(false)

const size = (bytes: number) => bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GB` : `${(bytes / 1024 ** 2).toFixed(1)} MB`

async function load() {
  loading.value = true
  try {
    usage.value = (await api.getStorageUsage()).data
  } catch {
    usage.value = null
  } finally {
    loading.value = false
  }
}

async function prune() {
  const what = includeResults.value
    ? L('HTML 报告和原始结果', 'HTML dashboards and raw results')
    : L('HTML 报告', 'HTML dashboards')
  try {
    await ElMessageBox.confirm(
      L(`将删除 ${days.value} 天前结束的运行的${what}。删除后无法恢复；运行的摘要和 AI 报告会保留。`,
        `This removes the ${what} of runs that ended more than ${days.value} days ago. It cannot be undone; summaries and AI reports are kept.`),
      L('清理旧报告？', 'Prune old reports?'),
      { type: 'warning', confirmButtonText: L('清理', 'Prune'), cancelButtonText: L('取消', 'Cancel') })
  } catch {
    return
  }
  pruning.value = true
  try {
    const { data } = await api.pruneStorage(days.value, includeResults.value)
    ElMessage.success(data.pruned_runs
      ? L(`已清理 ${data.pruned_runs} 次运行，释放 ${size(data.freed_bytes)}`, `Pruned ${data.pruned_runs} runs and freed ${size(data.freed_bytes)}`)
      : L('没有需要清理的运行', 'Nothing to prune'))
    await load()
  } catch (error: any) {
    ElMessage.error(error?.response?.data?.detail || L('清理失败', 'Pruning failed'))
  } finally {
    pruning.value = false
  }
}

onMounted(load)
defineExpose({ load, prune, usage, days, includeResults })
</script>

<style scoped lang="scss">
.storage { margin-top: 16px; }
.card-header { display: flex; justify-content: space-between; align-items: center; }
.hint { margin: 0 0 12px; font-size: 13px; line-height: 1.6; color: var(--app-text-secondary); }
.figures {
  display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin-bottom: 12px;
  @media (max-width: 700px) { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .label { display: block; font-size: 12px; color: var(--app-text-muted); }
  .num { font-size: 18px; font-variant-numeric: tabular-nums; }
}
.prune { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-size: 13px; }
</style>
