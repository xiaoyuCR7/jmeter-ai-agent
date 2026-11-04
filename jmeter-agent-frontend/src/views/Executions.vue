<template>
  <div class="executions-page">
    <div class="filters">
      <el-select v-model="projectId" :placeholder="L('全部项目', 'All projects')" clearable class="filter" @change="syncQuery">
        <el-option v-for="p in projects" :key="p.id" :label="p.name" :value="p.id" />
      </el-select>
      <el-select v-model="status" :placeholder="L('全部状态', 'All statuses')" clearable class="filter">
        <el-option v-for="s in STATUSES" :key="s" :label="t(`executions.status.${s}`)" :value="s" />
      </el-select>
      <span class="count num">{{ L(`共 ${total} 次执行`, `${total} runs`) }}</span>
      <el-button text @click="load"><el-icon><Refresh /></el-icon> {{ L('刷新', 'Refresh') }}</el-button>
    </div>

    <RunningRuns :executions="executions" />

    <section class="panel table-panel">
      <el-table :data="rows" v-loading="loading && !executions.length" class="clickable"
        :empty-text="L('还没有执行记录，在项目的测试计划里点「运行」', 'No runs yet. Use Run on a test plan.')"
        @row-click="(row: any) => OPENABLE.includes(row.status) && openReport(row.id)">
        <el-table-column :label="L('开始时间', 'Started')" width="172">
          <template #default="{ row }"><span class="num">{{ formatTime(row.start_time) }}</span></template>
        </el-table-column>
        <el-table-column :label="L('测试计划', 'Test plan')" min-width="200">
          <template #default="{ row }">
            <div class="plan" :title="row.test_plan_name">{{ row.test_plan_name || row.test_plan_id }}</div>
            <div v-if="!projectId" class="sub">{{ row.project_name }}</div>
          </template>
        </el-table-column>
        <el-table-column :label="L('状态', 'Status')" width="96">
          <template #default="{ row }">
            <StatusTag :status="row.status" />
            <el-progress v-if="row.status === 'running'" :percentage="row.progress || 0" :stroke-width="4" :show-text="false" />
            <el-tooltip v-if="row.status === 'failed' && row.error_message" :content="row.error_message" placement="top">
              <div class="sub error">{{ row.error_message }}</div>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column :label="L('需求判定', 'Verdict')" width="100">
          <template #default="{ row }"><VerdictTag v-if="row.metrics" :verdict="row.requirements" /></template>
        </el-table-column>
        <el-table-column label="Avg" width="80" align="right">
          <template #default="{ row }"><span class="num">{{ formatMs(row.metrics?.avg_response_time) }}</span></template>
        </el-table-column>
        <el-table-column label="P95" width="84" align="right">
          <template #default="{ row }"><span class="num">{{ formatMs(row.metrics?.p95_response_time) }}</span></template>
        </el-table-column>
        <el-table-column :label="L('错误率', 'Errors')" width="72" align="right">
          <template #default="{ row }">
            <span class="num" :class="{ bad: (row.metrics?.error_rate || 0) > 0 }">{{ formatPct(row.metrics?.error_rate) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="TPS" width="60" align="right">
          <template #default="{ row }"><span class="num">{{ formatTps(row.metrics?.throughput) }}</span></template>
        </el-table-column>
        <el-table-column :label="L('时长', 'Duration')" width="64" align="right">
          <template #default="{ row }"><span class="num">{{ formatDuration(row.start_time, row.end_time) }}</span></template>
        </el-table-column>
        <el-table-column :label="L('AI 分析', 'AI analysis')" width="84">
          <template #default="{ row }">
            <span v-if="row.has_ai_analysis" class="ai-done">{{ L('已分析', 'Done') }}</span>
            <span v-else class="sub">-</span>
          </template>
        </el-table-column>
        <el-table-column width="96" align="right">
          <template #default="{ row }">
            <div class="actions" @click.stop>
              <el-button v-if="row.status === 'running' || row.status === 'pending'" size="small" text type="warning" @click="cancel(row.id)">
                {{ L('取消', 'Cancel') }}
              </el-button>
              <el-button v-if="row.status === 'completed'" size="small" text type="primary" @click="openReport(row.id)">
                {{ L('报告', 'Report') }}
              </el-button>
              <el-dropdown trigger="click" @command="(cmd: string) => command(cmd, row)">
                <el-button size="small" text><el-icon><MoreFilled /></el-icon></el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="log">{{ L('查看日志', 'View log') }}</el-dropdown-item>
                    <el-dropdown-item command="markdown" :disabled="!row.has_markdown">{{ L('下载 AI 分析（Markdown）', 'Download AI analysis (Markdown)') }}</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </section>

    <el-dialog v-model="logVisible" :title="L('执行日志', 'Run log')" width="800px" style="max-width: calc(100vw - 32px)">
      <pre class="log-content">{{ logContent }}</pre>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { MoreFilled, Refresh } from '@element-plus/icons-vue'
import { api } from '@/api'
import { useI18n } from '@/composables/useI18n'
import { formatDuration, formatMs, formatPct, formatTime, formatTps } from '@/utils/format'
import StatusTag from '@/components/StatusTag.vue'
import VerdictTag from '@/components/VerdictTag.vue'
import RunningRuns from '@/components/RunningRuns.vue'

const STATUSES = ['running', 'pending', 'completed', 'failed', 'cancelled']
// A running run opens its live view; a finished one its report.
const OPENABLE = ['running', 'pending', 'completed']
const POLL_MS = 5000

const route = useRoute()
const router = useRouter()
const { t, L } = useI18n()

const projects = ref<any[]>([])
const executions = ref<any[]>([])
const total = ref(0)
const loading = ref(false)
const projectId = ref<string>((route.query.projectId as string) || '')
const status = ref('')
const logVisible = ref(false)
const logContent = ref('')
let pollTimer: ReturnType<typeof setTimeout> | undefined
let disposed = false

const rows = computed(() => (status.value ? executions.value.filter(e => e.status === status.value) : executions.value))

async function load() {
  const requested = projectId.value
  loading.value = true
  try {
    const res = await api.getExecutions(requested || undefined)
    if (requested !== projectId.value) return
    executions.value = res.data?.executions || []
    total.value = res.data?.total ?? executions.value.length
  } catch {
    if (requested === projectId.value) executions.value = []
  } finally {
    loading.value = false
  }
}

// Poll only while something is still running, so an idle page stays quiet.
function schedulePoll() {
  if (disposed) return
  pollTimer = setTimeout(async () => {
    if (executions.value.some(e => e.status === 'running' || e.status === 'pending')) await load()
    schedulePoll()
  }, POLL_MS)
}

function syncQuery() {
  router.replace({ query: projectId.value ? { projectId: projectId.value } : {} })
  load()
}

const openReport = (id: string) => router.push(`/executions/${id}`)

async function cancel(id: string) {
  try {
    await api.cancelExecution(id)
    ElMessage.success(L('已取消', 'Cancelled'))
    await load()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || L('取消失败', 'Cancel failed'))
  }
}

async function command(cmd: string, row: any) {
  if (cmd === 'log') {
    try {
      const res = await api.getLog(row.id, 200)
      logContent.value = res.data.log || L('暂无日志', 'No log yet')
    } catch {
      logContent.value = L('读取日志失败', 'Failed to read the log')
    }
    logVisible.value = true
  } else if (cmd === 'markdown') {
    try {
      const res = await api.downloadAnalysisReport(row.id)
      const url = URL.createObjectURL(new Blob([res.data], { type: 'text/markdown' }))
      const a = document.createElement('a')
      a.href = url
      a.download = `ai_analysis_${row.id}.md`
      a.click()
      setTimeout(() => URL.revokeObjectURL(url), 1000)
    } catch {
      ElMessage.error(L('下载失败', 'Download failed'))
    }
  }
}

onMounted(async () => {
  api.getProjects().then(res => { projects.value = res.data?.projects || res.data || [] }).catch(() => {})
  await load()
  schedulePoll()
})
onUnmounted(() => {
  disposed = true
  if (pollTimer !== undefined) clearTimeout(pollTimer)
})
</script>

<style scoped lang="scss">
.executions-page { max-width: 1440px; margin: 0 auto; display: flex; flex-direction: column; gap: 12px; }

.filters {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  .filter { width: 200px; }
  .count { margin-left: auto; font-size: 13px; color: var(--app-text-muted); }
}

.table-panel { padding: 0; overflow: hidden; }
.table-panel :deep(th.el-table__cell) { border-radius: 0; } // Flush with the panel, whose own corners clip it.
.plan { font-weight: 500; color: var(--app-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sub { font-size: 12px; color: var(--app-text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sub.error { color: var(--app-danger); max-width: 120px; }
.bad { color: var(--app-danger); }
.ai-done { font-size: 12px; color: var(--app-primary); }
.actions { display: flex; justify-content: flex-end; align-items: center; }
.clickable :deep(.el-table__row) { cursor: pointer; }

.log-content {
  max-height: 60vh;
  overflow: auto;
  margin: 0;
  padding: 12px;
  border-radius: var(--app-radius);
  background: var(--app-surface-muted);
  font-family: var(--app-font-mono);
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
