<template>
  <section v-if="runs.length" class="running">
    <div class="running-head">
      <span class="pulse" />
      {{ L(`正在运行 ${runs.length} 个`, `${runs.length} running`) }}
    </div>
    <div class="cards">
      <router-link v-for="run in runs" :key="run.id" :to="`/executions/${run.id}`" class="card">
        <div class="card-top">
          <span class="plan" :title="run.test_plan_name">{{ run.test_plan_name || run.test_plan_id }}</span>
          <el-tag v-if="purposeOf(run.id)" size="small" type="info">{{ purposeOf(run.id) }}</el-tag>
        </div>
        <template v-if="live[run.id]?.available && live[run.id]?.window?.requests">
          <div class="figures num">
            <span><b>{{ live[run.id]!.window!.throughput }}</b>/s</span>
            <span>P95 <b>{{ live[run.id]!.window!.p95 }}</b> ms</span>
            <span :class="{ bad: live[run.id]!.window!.error_rate > 1 }">{{ L('错误', 'Errors') }} <b>{{ live[run.id]!.window!.error_rate }}</b>%</span>
            <span>{{ L('线程', 'Threads') }} <b>{{ live[run.id]!.totals!.threads }}</b></span>
          </div>
        </template>
        <div v-else class="figures muted">
          {{ run.status === 'pending' ? L('排队等待开始', 'Waiting to start') : L('等待第一批结果', 'Waiting for the first results') }}
        </div>
        <el-progress v-if="live[run.id]?.planned_seconds" :percentage="live[run.id]!.progress" :stroke-width="4" :show-text="false" />
        <div class="time num muted">
          {{ L('已运行', 'Elapsed') }} {{ clock(live[run.id]?.elapsed_seconds) }}
          <template v-if="live[run.id]?.remaining_seconds != null"> · {{ L('剩余约', 'about') }} {{ clock(live[run.id]!.remaining_seconds) }}{{ L('', ' left') }}</template>
        </div>
      </router-link>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onUnmounted, ref, watch } from 'vue'
import { api, type LiveRun } from '@/api'
import { useI18n } from '@/composables/useI18n'

// The executions list rows; the ones still going get a card with what they are doing now.
const props = defineProps<{ executions: any[] }>()
const { L } = useI18n()

const POLL_MS = 3000
const live = ref<Record<string, LiveRun>>({})
const runs = computed(() => props.executions.filter(e => e.status === 'running' || e.status === 'pending'))
let timer: ReturnType<typeof setTimeout> | undefined
let disposed = false

function clock(seconds?: number | null) {
  if (seconds == null) return '—'
  const s = Math.max(0, Math.round(seconds)), m = Math.floor(s / 60)
  return m >= 60 ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}` : `${m}:${String(s % 60).padStart(2, '0')}`
}

function purposeOf(id: string) {
  const purpose = live.value[id]?.purpose
  if (purpose?.kind === 'capacity') return L(`容量探索 · 第 ${(purpose.step ?? 0) + 1} 级`, `Capacity · step ${(purpose.step ?? 0) + 1}`)
  if (purpose?.kind === 'verification') return L('验证运行', 'Verification')
  return ''
}

async function poll() {
  clearTimeout(timer)
  const ids = runs.value.map(run => run.id)
  const results = await Promise.allSettled(ids.map(id => api.getExecutionLive(id)))
  if (disposed) return
  const next: Record<string, LiveRun> = {}
  results.forEach((result, i) => {
    if (result.status === 'fulfilled') next[ids[i]] = result.value.data
    else if (live.value[ids[i]]) next[ids[i]] = live.value[ids[i]]  // Keep the last numbers through one missed poll.
  })
  live.value = next
  if (runs.value.length) timer = setTimeout(poll, POLL_MS)
}

watch(() => runs.value.map(run => run.id).join(','), ids => { if (ids) poll(); else clearTimeout(timer) }, { immediate: true })
onUnmounted(() => { disposed = true; clearTimeout(timer) })
</script>

<style scoped lang="scss">
.running { display: flex; flex-direction: column; gap: 8px; }
.running-head { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 600; color: var(--app-text); }
.pulse { width: 8px; height: 8px; border-radius: 50%; background: var(--app-warning); animation: pulse 1.4s ease-in-out infinite; }
@keyframes pulse { 50% { opacity: 0.25; } }
@media (prefers-reduced-motion: reduce) { .pulse { animation: none; } }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
.card {
  display: flex; flex-direction: column; gap: 8px; padding: 12px 14px; text-decoration: none; color: inherit;
  border: 1px solid var(--app-border); border-left: 3px solid var(--app-warning); border-radius: var(--app-radius-lg);
  background: var(--app-surface); transition: border-color 0.15s, box-shadow 0.15s;
  &:hover { border-color: var(--app-border-strong); box-shadow: var(--app-shadow-sm, none); }
  &:focus-visible { outline: 2px solid var(--app-primary); outline-offset: 2px; }
}
.card-top { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.plan { font-weight: 600; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.figures { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 13px; color: var(--app-text-secondary);
  b { color: var(--app-text); font-weight: 600; } }
.bad, .bad b { color: var(--app-danger); }
.muted { color: var(--app-text-muted); font-size: 12px; }
.num { font-variant-numeric: tabular-nums; }
</style>
