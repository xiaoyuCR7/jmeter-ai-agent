<template>
  <el-dialog :model-value="pending.length > 0" title="确认 Agent 操作 / Confirm agent action"
             :close-on-click-modal="false" :close-on-press-escape="false" :show-close="false" width="min(680px, 95vw)">
    <template v-if="scripts">
      <el-alert type="warning" :closable="false" show-icon
                title="这个计划会在本机以你的权限运行脚本或命令 / This plan runs scripts or commands on this machine with your privileges" />
      <p class="lead">它们可以读取你的文件、运行程序。只有确认计划来源可信时才允许；容器执行模式可以隔离这类脚本。<br>
        They can read your files and run programs. Allow only if you trust where the plan came from; container mode isolates them.</p>
      <p><span class="label">计划 / Plan</span><code :title="pending[0].arguments.jmx_path">{{ planName }}</code></p>
      <p class="label">将运行 / Will run</p>
      <ul class="findings">
        <li v-for="(item, index) in scripts" :key="index">{{ findingLabel(item) }}</li>
      </ul>
    </template>
    <template v-else-if="pending[0]">
      <p>Agent 正在等待确认。请核对工具和参数；拒绝或超时将停止这项操作。</p>
      <p><strong>{{ pending[0].tool }}</strong></p>
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
// The agent's second, script-specific approval (see _execute_with_script_approval) reads better as a list.
const scripts = computed<ScriptFinding[] | null>(() => {
  const findings = pending.value[0]?.arguments?.runs_scripts_on_this_machine
  return Array.isArray(findings) ? findings : null
})
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
.findings { max-height: 35vh; overflow: auto; margin: 0 0 12px; padding-left: 20px; line-height: 1.8; }
</style>
