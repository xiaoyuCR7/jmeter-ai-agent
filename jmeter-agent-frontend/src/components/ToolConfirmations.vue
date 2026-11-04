<template>
  <el-dialog :model-value="pending.length > 0" title="确认 Agent 操作 / Confirm agent action"
             :close-on-click-modal="false" :close-on-press-escape="false" :show-close="false" width="min(680px, 95vw)">
    <p v-if="pending[0]" class="origin">
      <span class="label">来源 / From</span>{{ sourceLabel }}
      <template v-if="pending[0].project"><span class="label project">项目 / Project</span>{{ pending[0].project }}</template>
    </p>
    <template v-if="scripts">
      <el-alert type="warning" :closable="false" show-icon
                title="这个计划会在本机以你的权限运行脚本或命令，或访问应用目录之外的文件 / This plan runs scripts or commands, or reaches files outside the app, with your privileges" />
      <p class="lead">只有确认计划来源可信时才允许；容器执行模式可以隔离这些操作。<br>
        Allow only if you trust where the plan came from; container mode isolates it.</p>
      <p><span class="label">计划 / Plan</span><code :title="pending[0].arguments.jmx_path">{{ planName }}</code></p>
      <p class="label">将运行 / Will run</p>
      <ul class="findings">
        <li v-for="(item, index) in scripts" :key="index">{{ findingLabel(item) }}</li>
      </ul>
    </template>
    <template v-else-if="pending[0]">
      <p>Agent 正在等待确认。请核对工具和参数；拒绝或超时将停止这项操作。</p>
      <p><strong>{{ pending[0].tool }}</strong></p>
      <el-alert v-if="production" type="warning" :closable="false" show-icon class="production"
                :title="`将在生产环境「${production.production_environment}」运行 / Runs against production: ${production.base_url || ''}`" />
      <table v-if="changes.length" class="changes">
        <thead><tr><th>项 / Field</th><th>当前 / Now</th><th>改为 / New</th></tr></thead>
        <tbody>
          <tr v-for="row in changes" :key="row.key">
            <td><code>{{ row.key }}</code></td><td>{{ row.now ?? '—' }}</td><td><strong>{{ row.next }}</strong></td>
          </tr>
        </tbody>
      </table>
      <pre>{{ JSON.stringify(pending[0].arguments, null, 2) }}</pre>
    </template>
    <el-alert v-if="error" :title="error" type="error" :closable="false" />
    <template #footer>
      <el-button :disabled="busy" @click="decide(false)">拒绝 / Deny</el-button>
      <el-button :type="scripts ? 'warning' : 'primary'" :loading="busy" @click="decide(true)">
        {{ scripts ? '仍然运行一次 / Run once anyway' : '确认执行一次 / Allow once' }}
      </el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { api } from '@/api'
import { findingLabel, type ScriptFinding } from '@/api/scriptApproval'
const pending = ref<any[]>([])
// Scripts a run would execute, listed by the run's own confirmation (details) or a later one.
const scripts = computed<ScriptFinding[] | null>(() => {
  const item = pending.value[0]
  const findings = item?.details?.runs_scripts_on_this_machine ?? item?.arguments?.runs_scripts_on_this_machine
  return Array.isArray(findings) ? findings : null
})
const production = computed(() => pending.value[0]?.details?.production_environment ? pending.value[0].details : null)
// What a change replaces, next to what it asks for, so "think_time_ms: 0" reads as "1000 → 0".
const changes = computed(() => {
  const item = pending.value[0]
  const current = item?.details?.current
  if (!current) return []
  return Object.keys(current).map(key => ({
    key, now: current[key], next: key === 'extractor' ? item.arguments?.json_path : item.arguments?.[key],
  }))
})
const sourceLabel = computed(() => pending.value[0]?.source === 'mcp'
  ? '外部 MCP 客户端 / External MCP client' : '本应用的 Agent / This app\'s agent')
const planName = computed(() => String(pending.value[0]?.arguments?.jmx_path || '').split(/[\\/]/).pop())
const busy = ref(false)
const error = ref('')
let stopped = false
let timer: ReturnType<typeof setTimeout> | undefined
async function poll() {
  try {
    const result = await api.pendingConfirmations()
    if (!stopped) pending.value = result.data
  } catch { /* Authentication errors are handled by the shared transport. */ }
  finally { if (!stopped) timer = setTimeout(poll, 5000) }
}
async function decide(approved: boolean) {
  const item = pending.value[0]
  if (!item || busy.value) return
  busy.value = true
  error.value = ''
  try {
    await api.decideConfirmation(item.id, approved)
    pending.value = pending.value.filter(request => request.id !== item.id)
  } catch {
    error.value = '确认未生效，可能已过期。请查看任务状态。 / Decision failed or expired.'
  } finally { busy.value = false }
}
onMounted(poll)
onUnmounted(() => { stopped = true; if (timer) clearTimeout(timer) })
</script>

<style scoped>
pre { max-height: 45vh; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px; }
p { margin-bottom: 12px; }
.lead { margin-top: 12px; color: var(--app-text-secondary); line-height: 1.6; }
.label { font-weight: 600; margin-right: 8px; }
.label.project { margin-left: 20px; }
.findings { max-height: 35vh; overflow: auto; margin: 0 0 12px; padding-left: 20px; line-height: 1.8; }
.production { margin-bottom: 12px; }
.changes { width: 100%; border-collapse: collapse; margin-bottom: 12px; font-size: 13px; }
.changes th, .changes td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--app-border, #e5e7eb); }
.changes th { color: var(--app-text-secondary); font-weight: 500; }
</style>
