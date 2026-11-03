<template>
  <section class="panel run-questions">
    <div class="panel-head"><h3>{{ L('继续追问', 'Ask about this run') }}</h3></div>
    <div v-for="(item, i) in items" :key="i" class="exchange">
      <p class="question">{{ item.question }}</p>
      <div class="answer markdown" v-html="md.render(item.answer || '')"></div>
      <el-alert v-if="item.mismatches?.length" type="error" :closable="false" show-icon
        :title="L('回答里有数字与实测不符，请以指标为准', 'Some numbers in this answer do not match the measurements; trust the metrics')"
        :description="item.mismatches.map((m: any) => `${m.metric}: ${m.claimed} → ${m.actual}`).join('；')" />
      <p v-if="item.corrected_from" class="note">
        {{ L(`初稿有 ${item.corrected_from} 处数字与实测不符，已按核对结果改写`, `The first draft had ${item.corrected_from} numbers that did not match; they were rewritten`) }}
      </p>
      <p v-if="usageText(item.usage, currentLang)" class="note">{{ usageText(item.usage, currentLang) }}</p>
    </div>
    <el-input v-model="question" type="textarea" :autosize="{ minRows: 2, maxRows: 6 }" maxlength="2000"
      :disabled="asking" :placeholder="L('例如：错误集中在哪个接口？下一步先查什么？', 'For example: which request caused most errors? What should I check first?')"
      @keydown.ctrl.enter="ask" />
    <p v-if="asking && progress" class="progress" aria-live="polite">{{ progress }}</p>
    <div class="actions">
      <span class="hint">{{ L('回答只读这次运行的证据，不会改动任何内容', 'Answers read this run\'s evidence and change nothing') }}</span>
      <el-button type="primary" :loading="asking" :disabled="!question.trim()" @click="ask">
        {{ asking ? L('正在分析', 'Analysing') : L('提问', 'Ask') }}
      </el-button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onUnmounted, ref } from 'vue'
import MarkdownIt from 'markdown-it'
import { ElMessage } from 'element-plus'
import { api } from '@/api'
import { readEvents } from '@/api/sse'
import { useI18n } from '@/composables/useI18n'
import { usageText } from '@/utils/format'

const props = defineProps<{ executionId: string; followUps?: any[] }>()

const { currentLang } = useI18n()
const L = (zh: string, en: string) => currentLang.value === 'en' ? en : zh

const md = new MarkdownIt()  // Raw HTML stays escaped: the answer is model text.
const items = ref<any[]>([...(props.followUps || [])])
const question = ref('')
const asking = ref(false)
const progress = ref('')
let controller: AbortController | null = null
// Leaving the page stops the answer: the server cancels the model when the stream closes.
onUnmounted(() => controller?.abort())

async function ask() {
  const text = question.value.trim()
  if (!text || asking.value) return
  asking.value = true
  progress.value = ''
  controller = new AbortController()
  const failed = L('追问失败，请稍后再试', 'The question failed. Try again later.')
  try {
    const response = await api.askAboutRunStream(props.executionId, text, controller.signal)
    if (!response.ok) {
      const body = await response.json().catch(() => ({}))
      throw new Error(typeof body.detail === 'string' ? body.detail : failed)
    }
    let answer: any = null
    let error = ''
    await readEvents(response, (event) => {
      if (event.step === 'progress') progress.value = event.message
      else if (event.step === 'answer') answer = event.answer
      else if (event.step === 'error') error = event.message
    })
    if (!answer) throw new Error(error || failed)
    items.value.push(answer)
    question.value = ''
  } catch (e: any) {
    if (e?.name !== 'AbortError') ElMessage.error(e?.message || failed)
  } finally {
    asking.value = false
    controller = null
  }
}
</script>

<style scoped lang="scss">
.run-questions { display: flex; flex-direction: column; gap: 12px; }
.exchange { display: flex; flex-direction: column; gap: 6px; padding-bottom: 12px; border-bottom: 1px solid var(--app-border); }
.question { margin: 0; font-weight: 600; color: var(--app-text); white-space: pre-wrap; }
.answer { line-height: 1.7; color: var(--app-text-secondary); }
.answer :deep(p), .answer :deep(ul), .answer :deep(ol) { margin: 0 0 6px; }
.answer :deep(ul), .answer :deep(ol) { padding-left: 20px; }
.actions { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.note { margin: 0; font-size: 12px; color: var(--app-text-muted); }
.progress { margin: 0; font-size: 13px; color: var(--app-text-secondary); }
.hint { font-size: 12px; color: var(--app-text-muted); }
</style>
