<template>
  <section class="panel run-memory" v-loading="loading">
    <div class="panel-head">
      <h3>{{ L('经验与假设', 'Lessons and hypotheses') }}</h3>
      <el-button size="small" @click="emit('save-lesson')">{{ L('存为经验', 'Save a lesson') }}</el-button>
    </div>
    <p class="hint">{{ L('同一项目以后的报告和追问会读到这里的内容。', 'Later reports and questions in this project read what is here.') }}</p>

    <h4>{{ L(`相关假设 ${hypotheses.length}`, `Hypotheses ${hypotheses.length}`) }}</h4>
    <p v-if="!hypotheses.length" class="muted">{{ L('还没有关于这个计划或这些接口的假设。', 'No hypotheses about this plan or these requests yet.') }}</p>
    <article v-for="hyp in hypotheses" :key="hyp.id" class="item">
      <div class="item-head">
        <el-tag :type="statusType(hyp.status)" size="small">{{ statusText(hyp.status) }}</el-tag>
        <span class="statement">{{ hyp.statement }}</span>
      </div>
      <p v-if="hyp.prediction" class="detail">
        {{ hyp.status !== 'untested' ? L('预测：', 'Prediction: ')
           : hyp.checkable ? L('下次符合条件的运行会自动验证：', 'Checked automatically by the next matching run: ')
           : L('程序无法验证，需要人工判断：', 'No program can check this; decide it yourself: ') }}{{ hyp.prediction }}
      </p>
      <p v-if="hyp.notes" class="detail">{{ hyp.notes }}</p>
      <div v-if="hyp.status === 'untested'" class="actions">
        <el-button v-if="hyp.checkable" type="primary" text size="small" :loading="busy === hyp.id" @click="verify(hyp)">
          {{ L('运行验证', 'Run a check') }}
        </el-button>
        <el-button text size="small" :loading="busy === hyp.id" @click="decide(hyp, 'confirmed')">{{ L('预测成立', 'It held') }}</el-button>
        <el-button text size="small" :loading="busy === hyp.id" @click="decide(hyp, 'refuted')">{{ L('预测不成立', 'It did not hold') }}</el-button>
        <el-button text size="small" :loading="busy === hyp.id" @click="decide(hyp, 'deprecated')">{{ L('作废', 'Discard') }}</el-button>
      </div>
    </article>

    <h4>{{ L(`会被引用的经验 ${lessons.length}`, `Lessons used ${lessons.length}`) }}</h4>
    <p v-if="!lessons.length" class="muted">{{ L('还没有经验。把报告里确认过的结论存为经验，之后的报告就能用上。', 'No lessons yet. Save a conclusion you trust and later reports will use it.') }}</p>
    <article v-for="lesson in lessons" :key="lesson.id" class="item">
      <div class="item-head">
        <el-tag size="small" type="info">{{ kindText(lesson.kind) }}</el-tag>
        <span class="statement">{{ lesson.title }}</span>
        <el-tag v-if="lesson.needs_review" size="small" type="warning">{{ L('需复核', 'Review') }}</el-tag>
      </div>
      <p class="detail">{{ lesson.content }}</p>
      <p class="meta">
        {{ lesson.labels.length ? lesson.labels.join('、') : L('整个项目', 'Whole project') }}
        · {{ L(`被引用 ${lesson.cited_count} 次`, `Used ${lesson.cited_count} times`) }}
      </p>
      <div class="actions">
        <el-button text size="small" :loading="busy === lesson.id" @click="retire(lesson)">{{ L('停用', 'Retire') }}</el-button>
      </div>
    </article>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api'
import { useI18n } from '@/composables/useI18n'
import { useRouter } from 'vue-router'

const props = defineProps<{ executionId: string; refreshKey?: number }>()
const emit = defineEmits<{ 'save-lesson': [] }>()

const { currentLang } = useI18n()
const L = (zh: string, en: string) => currentLang.value === 'en' ? en : zh

const loading = ref(false)
const busy = ref('')
const hypotheses = ref<any[]>([])
const lessons = ref<any[]>([])

const STATUS: Record<string, { zh: string; en: string; type: 'info' | 'success' | 'danger' | 'warning' }> = {
  untested: { zh: '待验证', en: 'Open', type: 'warning' },
  confirmed: { zh: '预测成立', en: 'Held', type: 'success' },
  refuted: { zh: '预测不成立', en: 'Did not hold', type: 'danger' },
  deprecated: { zh: '已作废', en: 'Discarded', type: 'info' },
}
const statusText = (status: string) => STATUS[status] ? L(STATUS[status].zh, STATUS[status].en) : status
const DECIDE: Record<string, [string, string]> = {
  confirmed: ['判定为预测成立', 'Mark as held'], refuted: ['判定为预测不成立', 'Mark as not held'],
  deprecated: ['作废这条假设', 'Discard this hypothesis'],
}
const statusType = (status: string) => STATUS[status]?.type ?? 'info'
const kindText = (kind: string) => ({
  known_issue: L('已知问题', 'Known issue'), defect_retrospective: L('缺陷复盘', 'Defect review'),
  module_standard: L('接口标准', 'Request standard'),
}[kind] ?? kind)

async function load() {
  loading.value = true
  try {
    const data = (await api.getRunMemory(props.executionId)).data
    hypotheses.value = data.hypotheses
    lessons.value = data.experiences
  } catch {
    ElMessage.error(L('读取经验与假设失败', 'Could not load lessons and hypotheses'))
  } finally {
    loading.value = false
  }
}

const router = useRouter()

async function verify(hyp: any) {
  try {
    await ElMessageBox.confirm(
      L(`将按预测的负载重新运行这条假设的测试计划：${hyp.prediction}。运行结束后程序自动判定。`,
        `This reruns the hypothesis' plan at the load its prediction names: ${hyp.prediction}. The run settles it when it ends.`),
      L('运行验证', 'Run a check'), { confirmButtonText: L('开始运行', 'Start the run'), type: 'info' })
  } catch {
    return
  }
  busy.value = hyp.id
  try {
    const run = (await api.startVerificationRun(hyp.id)).data
    ElMessage.success(L('验证运行已开始', 'The check run has started'))
    router.push(`/executions/${run.id}`)
  } catch (e: any) {
    const detail = e.response?.data?.detail
    ElMessage.error((typeof detail === 'string' ? detail : detail?.message) || L('无法开始验证运行', 'Could not start the check run'))
  } finally {
    busy.value = ''
  }
}

async function decide(hyp: any, status: 'confirmed' | 'refuted' | 'deprecated') {
  let note = ''
  try {
    note = (await ElMessageBox.prompt(L('写一句依据，之后的报告会看到它（可留空）', 'Say why; later reports will see it (optional)'),
      L(...DECIDE[status]), { confirmButtonText: L('保存判定', 'Save decision'), inputPlaceholder: L('例如：加大连接池后重测，P95 没有变化', 'e.g. retested with a larger pool; P95 did not change') }) as any).value || ''
  } catch {
    return
  }
  busy.value = hyp.id
  try {
    Object.assign(hyp, (await api.decideHypothesis(hyp.id, status, note)).data)
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || L('保存判定失败', 'Could not save the decision'))
  } finally {
    busy.value = ''
  }
}

async function retire(lesson: any) {
  let reason = ''
  try {
    reason = (await ElMessageBox.prompt(L('停用后它不再进入报告，可以在设置的“记忆”里恢复。原因（可留空）：',
      'Retired lessons stay out of reports; you can restore them under Settings > Memory. Reason (optional):'),
      L('停用经验', 'Retire lesson')) as any).value || ''
  } catch {
    return
  }
  busy.value = lesson.id
  try {
    await api.setLessonStatus(lesson.id, 'retired', reason)
    lessons.value = lessons.value.filter(item => item.id !== lesson.id)
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || L('停用失败', 'Could not retire it'))
  } finally {
    busy.value = ''
  }
}

onMounted(load)
watch(() => props.refreshKey, load)
</script>

<style scoped lang="scss">
.run-memory { display: flex; flex-direction: column; gap: 10px; }
.panel-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.panel-head h3 { margin: 0; }
h4 { margin: 8px 0 0; font-size: 14px; color: var(--app-text); }
.hint, .muted, .meta { margin: 0; font-size: 12px; color: var(--app-text-muted); }
.item { display: flex; flex-direction: column; gap: 4px; padding: 10px 0; border-bottom: 1px solid var(--app-border); }
.item-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.statement { font-weight: 600; color: var(--app-text); }
.detail { margin: 0; line-height: 1.6; color: var(--app-text-secondary); white-space: pre-wrap; }
.actions { display: flex; gap: 4px; flex-wrap: wrap; }
</style>
