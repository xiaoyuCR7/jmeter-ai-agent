<template>
  <section class="panel run-questions">
    <div class="panel-head"><h3>{{ L('继续追问', 'Ask about this run') }}</h3></div>
    <div v-for="(item, i) in items" :key="i" class="exchange">
      <p class="question">{{ item.question }}</p>
      <p class="answer">{{ item.answer }}</p>
      <el-alert v-if="item.mismatches?.length" type="error" :closable="false" show-icon
        :title="L('回答里有数字与实测不符，请以指标为准', 'Some numbers in this answer do not match the measurements; trust the metrics')"
        :description="item.mismatches.map((m: any) => `${m.metric}: ${m.claimed} → ${m.actual}`).join('；')" />
    </div>
    <el-input v-model="question" type="textarea" :autosize="{ minRows: 2, maxRows: 6 }" maxlength="2000"
      :disabled="asking" :placeholder="L('例如：错误集中在哪个接口？下一步先查什么？', 'For example: which request caused most errors? What should I check first?')"
      @keydown.ctrl.enter="ask" />
    <div class="actions">
      <span class="hint">{{ L('回答只读这次运行的证据，不会改动任何内容', 'Answers read this run\'s evidence and change nothing') }}</span>
      <el-button type="primary" :loading="asking" :disabled="!question.trim()" @click="ask">
        {{ asking ? L('正在分析', 'Analysing') : L('提问', 'Ask') }}
      </el-button>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api'
import { useI18n } from '@/composables/useI18n'

const props = defineProps<{ executionId: string; followUps?: any[] }>()

const { currentLang } = useI18n()
const L = (zh: string, en: string) => currentLang.value === 'en' ? en : zh

const items = ref<any[]>([...(props.followUps || [])])
const question = ref('')
const asking = ref(false)

async function ask() {
  const text = question.value.trim()
  if (!text || asking.value) return
  asking.value = true
  try {
    items.value.push((await api.askAboutRun(props.executionId, text)).data)
    question.value = ''
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || L('追问失败，请稍后再试', 'The question failed. Try again later.'))
  } finally {
    asking.value = false
  }
}
</script>

<style scoped lang="scss">
.run-questions { display: flex; flex-direction: column; gap: 12px; }
.exchange { display: flex; flex-direction: column; gap: 6px; padding-bottom: 12px; border-bottom: 1px solid var(--app-border); }
.question { margin: 0; font-weight: 600; color: var(--app-text); white-space: pre-wrap; }
.answer { margin: 0; line-height: 1.7; color: var(--app-text-secondary); white-space: pre-wrap; }
.actions { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.hint { font-size: 12px; color: var(--app-text-muted); }
</style>
