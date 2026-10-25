<template>
  <section class="panel">
    <el-result icon="success" :title="title" :sub-title="detail">
      <template #extra>
        <el-button type="primary" @click="emit('review')">{{ L('核对性能需求', 'Review requirements') }}</el-button>
        <el-button @click="router.push(`/projects/${projectId}/plans`)">{{ L('查看测试计划', 'View test plans') }}</el-button>
      </template>
    </el-result>
    <div v-if="diagnostic" class="diagnostic">
      <h4>{{ L('AI 说明', 'AI notes') }}</h4>
      <div class="markdown" v-html="md.render(diagnostic)"></div>
    </div>
    <div class="actions"><el-button text @click="emit('reset')">{{ L('再导入一份', 'Start another') }}</el-button></div>
  </section>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router'
import MarkdownIt from 'markdown-it'
import { useI18n } from '@/composables/useI18n'

defineProps<{ projectId: string; title: string; detail: string; diagnostic?: string }>()
const emit = defineEmits<{ review: []; reset: [] }>()
const md = new MarkdownIt()
const router = useRouter()
const { currentLang } = useI18n()
const L = (zh: string, en: string) => currentLang.value === 'en' ? en : zh
</script>

<style scoped lang="scss">
.diagnostic {
  border-top: 1px solid var(--app-border);
  padding-top: 12px;
  h4 { margin: 0 0 8px; font-size: 14px; color: var(--app-text); }
  .markdown { color: var(--app-text-secondary); font-size: 13px; }
}
.actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 8px; }
</style>
