<template>
  <el-dialog :model-value="modelValue" :title="L('容量探索', 'Capacity search')" width="860px" top="6vh" destroy-on-close
    @update:model-value="emit('update:modelValue', $event)">
    <p class="hint">
      {{ L('从起始线程数开始逐级加压，每一级是一次完整运行。遇到性能需求不达标、错误率超限或吞吐量不再随负载增长时停下，上一级就是这个计划能承受的负载。计划文件本身不会被修改。为节省模型调用，只在探索结束时为停下的那一级写一份 AI 报告。',
           'Raises the load step by step, each step a full run. It stops at the first step that fails a requirement, errors too much or stops gaining throughput; the step before is the load the plan takes. The plan file is not changed. To save model calls, one AI report is written when the search ends, on the step that stopped it.') }}
    </p>
    <el-form :inline="true" :model="form" size="default" class="form" :disabled="running">
      <el-form-item :label="L('起始线程', 'Start threads')">
        <el-input-number v-model="form.start_threads" :min="1" :max="100000" controls-position="right" />
      </el-form-item>
      <el-form-item :label="L('最大线程', 'Max threads')">
        <el-input-number v-model="form.max_threads" :min="1" :max="100000" controls-position="right" />
      </el-form-item>
      <el-form-item :label="L('每步倍数', 'Factor')">
        <el-input-number v-model="form.factor" :min="1.1" :max="4" :step="0.1" :precision="1" controls-position="right" />
      </el-form-item>
      <el-form-item :label="L('错误率上限 %', 'Max error %')">
        <el-input-number v-model="form.max_error_rate" :min="0" :max="100" :step="0.5" controls-position="right" />
      </el-form-item>
      <el-button type="primary" :loading="starting" :disabled="running" @click="start">{{ L('开始探索', 'Start') }}</el-button>
    </el-form>

    <template v-if="current">
      <div class="result">
        <el-tag :type="statusType(current.status)">{{ statusText(current.status) }}</el-tag>
        <span v-if="current.capacity_threads" class="capacity">
          {{ L(`可承受 ${current.capacity_threads} 线程，吞吐量 ${fixed(current.capacity_throughput)} 请求/秒`,
               `Takes ${current.capacity_threads} threads at ${fixed(current.capacity_throughput)} requests/s`) }}
        </span>
        <span v-else-if="current.status !== 'running'" class="capacity muted">
          {{ L('起始负载就没有通过', 'The first step did not pass') }}
        </span>
        <span class="muted">{{ current.stop_reason }}</span>
        <router-link v-if="current.report_execution_id" :to="`/executions/${current.report_execution_id}`">
          {{ L('AI 分析报告', 'AI report') }}
        </router-link>
        <el-button v-if="current.status === 'running'" size="small" :loading="cancelling" @click="cancel">{{ L('取消', 'Cancel') }}</el-button>
      </div>
      <el-table :data="current.steps" size="small">
        <el-table-column :label="L('线程', 'Threads')" prop="threads" width="80" align="right" />
        <el-table-column :label="L('状态', 'Status')" width="100">
          <template #default="{ row }"><el-tag size="small" :type="stepType(row.status)">{{ stepText(row.status) }}</el-tag></template>
        </el-table-column>
        <el-table-column :label="L('吞吐量', 'Throughput')" width="110" align="right">
          <template #default="{ row }"><span class="num">{{ fixed(row.throughput) }}</span></template>
        </el-table-column>
        <el-table-column label="P95 (ms)" width="100" align="right">
          <template #default="{ row }"><span class="num">{{ fixed(row.p95, 0) }}</span></template>
        </el-table-column>
        <el-table-column :label="L('错误率', 'Errors')" width="90" align="right">
          <template #default="{ row }"><span class="num">{{ row.error_rate == null ? '—' : `${fixed(row.error_rate)}%` }}</span></template>
        </el-table-column>
        <el-table-column :label="L('需求', 'Requirements')" width="100">
          <template #default="{ row }">{{ row.requirements || '—' }}</template>
        </el-table-column>
        <el-table-column :label="L('说明', 'Note')" min-width="160">
          <template #default="{ row }">
            <router-link v-if="row.execution_id && row.status !== 'pending'" :to="`/executions/${row.execution_id}`">
              {{ row.note || L('查看报告', 'Report') }}
            </router-link>
            <span v-else>{{ row.note }}</span>
          </template>
        </el-table-column>
      </el-table>
    </template>

    <div v-if="history.length > 1" class="history">
      <span class="muted">{{ L('之前的探索：', 'Earlier searches: ') }}</span>
      <el-button v-for="item in history" :key="item.id" text size="small" :type="item.id === current?.id ? 'primary' : ''"
        @click="current = item">
        {{ formatTime(item.created_at) }} · {{ item.capacity_threads ?? '—' }}
      </el-button>
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'
import { api, type CapacitySearch } from '@/api'
import { useI18n } from '@/composables/useI18n'
import { formatTime } from '@/utils/format'

const props = defineProps<{ modelValue: boolean; plan: any | null }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()
const { L } = useI18n()

const form = reactive({ start_threads: 10, max_threads: 100, factor: 1.5, max_error_rate: 1 })
const history = ref<CapacitySearch[]>([])
const current = ref<CapacitySearch | null>(null)
const starting = ref(false)
const cancelling = ref(false)
const running = computed(() => current.value?.status === 'running')
let timer: ReturnType<typeof setInterval> | null = null

const fixed = (value: number | null | undefined, digits = 1) => value == null ? '—' : value.toFixed(digits)
const STATUS: Record<string, [string, string, any]> = {
  running: ['进行中', 'Running', 'warning'], finished: ['已完成', 'Finished', 'success'],
  failed: ['未完成', 'Failed', 'danger'], cancelled: ['已取消', 'Cancelled', 'info'],
}
const STEP: Record<string, [string, string, any]> = {
  pending: ['等待', 'Waiting', 'info'], running: ['运行中', 'Running', 'warning'], passed: ['通过', 'Passed', 'success'],
  stopped: ['停止点', 'Stop', 'danger'], failed: ['失败', 'Failed', 'danger'], skipped: ['未运行', 'Not run', 'info'],
}
const statusText = (status: string) => STATUS[status] ? L(STATUS[status][0], STATUS[status][1]) : status
const statusType = (status: string) => STATUS[status]?.[2] ?? 'info'
const stepText = (status: string) => STEP[status] ? L(STEP[status][0], STEP[status][1]) : status
const stepType = (status: string) => STEP[status]?.[2] ?? 'info'

async function load() {
  if (!props.plan) return
  const users = Number(props.plan.test_config?.concurrent_users) || 10
  form.start_threads = Math.max(1, Math.round(users / 2))
  form.max_threads = Math.max(form.start_threads + 1, users * 4)
  try {
    history.value = (await api.listCapacitySearches(props.plan.id)).data
    current.value = history.value[0] ?? null
  } catch {
    history.value = []
  }
  poll()
}

function poll() {
  stopPolling()
  if (!running.value) return
  timer = setInterval(async () => {
    if (!current.value) return
    try {
      current.value = (await api.getCapacitySearch(current.value.id)).data
      if (!running.value) stopPolling()
    } catch {
      stopPolling()
    }
  }, 5000)
}

function stopPolling() {
  if (timer) clearInterval(timer)
  timer = null
}

async function start() {
  if (!props.plan) return
  if (form.max_threads < form.start_threads) {
    ElMessage.error(L('最大线程数不能小于起始线程数', 'Max threads cannot be below the start'))
    return
  }
  starting.value = true
  try {
    current.value = (await api.startCapacitySearch(props.plan.id, { ...form })).data
    history.value = [current.value, ...history.value]
    poll()
  } catch (e: any) {
    const detail = e.response?.data?.detail
    ElMessage.error((typeof detail === 'string' ? detail : detail?.message) || L('无法开始探索', 'Could not start the search'))
  } finally {
    starting.value = false
  }
}

async function cancel() {
  if (!current.value) return
  cancelling.value = true
  try {
    await api.cancelCapacitySearch(current.value.id)
    current.value = (await api.getCapacitySearch(current.value.id)).data
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || L('取消失败', 'Could not cancel'))
  } finally {
    cancelling.value = false
  }
}

watch(() => [props.modelValue, props.plan?.id], ([open]) => { if (open) load(); else stopPolling() }, { immediate: true })
onUnmounted(stopPolling)
</script>

<style scoped lang="scss">
.hint { color: var(--app-text-muted); font-size: 13px; margin: 0 0 12px; }
.form { margin-bottom: 8px; }
.result { display: flex; align-items: center; gap: 12px; margin: 8px 0 12px; flex-wrap: wrap; }
.capacity { font-weight: 600; color: var(--app-text); }
.muted { color: var(--app-text-muted); font-size: 13px; }
.num { font-variant-numeric: tabular-nums; }
.history { margin-top: 12px; display: flex; align-items: center; flex-wrap: wrap; gap: 4px; }
</style>
