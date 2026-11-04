<template>
  <el-dialog :model-value="modelValue" :title="L('试跑', 'Trial run')" width="860px" top="6vh" destroy-on-close
    @update:model-value="emit('update:modelValue', $event)">
    <p class="hint">
      {{ L('用 1 个用户把计划走一遍，每个请求只发一次，在当前环境下检查接口能不能通、鉴权和变量提取对不对。不记入运行历史。',
           'One user goes through the plan once, each request sent a single time, against the active environment, to check that the requests work and that authentication and extracted variables are right. Nothing is added to the run history.') }}
    </p>
    <div class="bar">
      <el-select v-if="plans.length > 1" v-model="planId" size="default" style="width: 320px" :disabled="busy">
        <el-option v-for="plan in plans" :key="plan.id" :label="plan.name" :value="plan.id" />
      </el-select>
      <el-button type="primary" :loading="running" :disabled="busy || !planId" @click="start">
        {{ result ? L('再试跑一次', 'Run the trial again') : L('开始试跑', 'Start the trial') }}
      </el-button>
      <span v-if="running" class="muted">{{ L('压测机正在运行其它测试时会先排队', 'Waits in line while the load generator runs another test') }}</span>
    </div>

    <template v-if="result">
      <el-alert v-if="result.error" type="error" :closable="false" :title="L('试跑没有完成', 'The trial did not finish')"
        :description="result.error" show-icon />
      <p v-else class="verdict" :class="result.failed ? 'bad' : 'good'">
        {{ result.failed
          ? L(`${result.failed} 个请求失败，${result.passed} 个成功`, `${result.failed} requests failed, ${result.passed} succeeded`)
          : L(`全部 ${result.passed} 个请求都成功了，可以正式运行`, `All ${result.passed} requests succeeded; the plan is ready to run`) }}
        <span class="muted">· {{ result.duration_seconds }}s{{ result.environment ? ` · ${L('环境', 'environment')} ${result.environment}` : '' }}</span>
      </p>

      <el-table v-if="result.samples.length" :data="result.samples" size="small" row-key="label" max-height="320">
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="detail">
              <div class="url">{{ row.method }} {{ row.url }}</div>
              <div v-for="failure in row.failures" :key="failure" class="failure">{{ failure }}</div>
              <pre>{{ row.response || L('（没有响应内容）', '(empty response)') }}{{ row.response_truncated ? '\n…' : '' }}</pre>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="L('请求', 'Request')" prop="label" min-width="240" />
        <el-table-column :label="L('状态码', 'Code')" prop="code" width="80" />
        <el-table-column :label="L('结果', 'Result')" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.success ? 'success' : 'danger'">{{ row.success ? L('成功', 'OK') : L('失败', 'Failed') }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="L('耗时', 'Time')" width="90" align="right">
          <template #default="{ row }"><span class="num">{{ row.elapsed_ms }} ms</span></template>
        </el-table-column>
        <el-table-column :label="L('说明', 'Message')" min-width="160">
          <template #default="{ row }">{{ row.failures[0] || row.message }}</template>
        </el-table-column>
      </el-table>

      <div v-if="result.failed" class="diagnosis">
        <el-button :loading="diagnosing" :disabled="busy" @click="diagnose">
          {{ diagnosis ? L('重新诊断', 'Diagnose again') : L('让 AI 诊断失败原因', 'Ask AI why they failed') }}
        </el-button>
        <template v-if="diagnosis">
          <div class="markdown" v-html="md.render(diagnosis.summary)"></div>
          <ul v-if="diagnosis.advice.length" class="advice">
            <li v-for="item in diagnosis.advice" :key="item">{{ item }}</li>
          </ul>
          <div v-for="fix in diagnosis.fixes" :key="fix.id" class="fix">
            <div class="fix-text">
              <strong>{{ fix.sampler }}</strong>
              {{ fix.action === 'add'
                ? L(`新增提取 ${fix.variable} = ${fix.expression}`, `extract ${fix.variable} = ${fix.expression}`)
                : L(`提取 ${fix.variable}：${fix.current ?? '?'} → ${fix.expression}`, `extract ${fix.variable}: ${fix.current ?? '?'} → ${fix.expression}`) }}
              <div class="muted">{{ fix.reason }}</div>
              <div class="muted">
                {{ fix.verified
                  ? L(`试跑响应里这个路径的值：${fix.preview}`, `Value at this path in the trial response: ${fix.preview}`)
                  : L('试跑响应里找不到这个路径，只能手动核对', 'The trial response has no value at this path; check it by hand') }}
              </div>
            </div>
            <el-button v-if="fix.applicable" size="small" type="primary" :loading="applying === fix.id"
              :disabled="busy || applied.includes(fix.id)" @click="apply(fix)">
              {{ applied.includes(fix.id) ? L('已应用', 'Applied') : L('应用', 'Apply') }}
            </el-button>
          </div>
          <p v-if="applied.length" class="muted">{{ L('计划已修改，再试跑一次确认效果。', 'The plan changed; run the trial again to check.') }}</p>
          <p v-if="usageText(diagnosis.usage, currentLang)" class="muted">{{ usageText(diagnosis.usage, currentLang) }}</p>
        </template>
      </div>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import MarkdownIt from 'markdown-it'
import { ElMessage } from 'element-plus'
import { api, type TrialDiagnosis, type TrialFix, type TrialResult } from '@/api'
import { useI18n } from '@/composables/useI18n'
import { usageText } from '@/utils/format'

const props = defineProps<{ modelValue: boolean; plans: { id: string; name: string }[]; initialPlanId?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: boolean]; changed: [] }>()
const { L, currentLang } = useI18n()
const md = new MarkdownIt()

const planId = ref('')
const result = ref<TrialResult | null>(null)
const diagnosis = ref<TrialDiagnosis | null>(null)
const applied = ref<number[]>([])
const running = ref(false)
const diagnosing = ref(false)
const applying = ref<number | null>(null)
const busy = computed(() => running.value || diagnosing.value || applying.value !== null)

watch(() => [props.modelValue, props.initialPlanId], () => {
  if (!props.modelValue) return
  planId.value = props.initialPlanId || props.plans[0]?.id || ''
  result.value = diagnosis.value = null
}, { immediate: true })
watch(planId, () => { result.value = diagnosis.value = null })

const errorText = (e: any, fallback: string) => {
  const detail = e?.response?.data?.detail
  return typeof detail === 'string' ? detail : detail?.message || fallback
}

async function start() {
  running.value = true
  diagnosis.value = null
  applied.value = []
  try {
    result.value = (await api.trialRun(planId.value)).data
  } catch (e: any) {
    ElMessage.error(errorText(e, L('试跑失败', 'The trial failed')))
  } finally {
    running.value = false
  }
}

async function diagnose() {
  diagnosing.value = true
  try {
    diagnosis.value = (await api.diagnoseTrial(planId.value)).data
    applied.value = []
  } catch (e: any) {
    ElMessage.error(errorText(e, L('诊断失败', 'The diagnosis failed')))
  } finally {
    diagnosing.value = false
  }
}

async function apply(fix: TrialFix) {
  applying.value = fix.id
  try {
    await api.applyTrialFix(planId.value, fix.id)
    applied.value.push(fix.id)
    emit('changed')
  } catch (e: any) {
    ElMessage.error(errorText(e, L('应用失败', 'Could not apply it')))
  } finally {
    applying.value = null
  }
}
</script>

<style scoped lang="scss">
.hint { margin: 0 0 12px; color: var(--app-text-secondary); font-size: 13px; }
.bar { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
.muted { color: var(--app-text-muted); font-size: 12px; }
.verdict { margin: 4px 0 8px; font-weight: 500; &.good { color: var(--el-color-success); } &.bad { color: var(--el-color-danger); } }
.detail {
  padding: 4px 16px;
  .url { font-family: var(--app-font-mono, monospace); font-size: 12px; word-break: break-all; }
  .failure { color: var(--el-color-danger); font-size: 12px; margin-top: 4px; }
  pre { margin: 8px 0 0; max-height: 220px; overflow: auto; white-space: pre-wrap; word-break: break-all;
        font-size: 12px; background: var(--app-surface-muted, var(--el-fill-color-light)); padding: 8px; border-radius: 4px; }
}
.diagnosis {
  margin-top: 16px; border-top: 1px solid var(--app-border); padding-top: 12px;
  .markdown { margin-top: 12px; font-size: 13px; color: var(--app-text-secondary); }
  .advice { margin: 8px 0; padding-left: 20px; font-size: 13px; }
}
.fix {
  display: flex; justify-content: space-between; align-items: flex-start; gap: 12px;
  padding: 8px 0; border-top: 1px dashed var(--app-border); font-size: 13px;
  .fix-text { min-width: 0; word-break: break-all; }
}
.num { font-variant-numeric: tabular-nums; }
</style>
