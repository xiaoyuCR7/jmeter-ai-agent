<template>
  <div class="start-page">
    <header class="intro">
      <h2>{{ L('从文档开始', 'Start from a document') }}</h2>
      <p>{{ L('AI 根据文档生成项目和测试计划，再从中提取性能需求草稿供你核对。', 'AI builds the project and test plans from your document, then drafts performance requirements for you to review.') }}</p>
    </header>

    <ol class="stepper">
      <li v-for="(label, i) in stepLabels" :key="i" :class="{ active: stage === i, done: stage > i }">
        <span class="index">{{ i + 1 }}</span>{{ label }}
      </li>
    </ol>

    <SourceStep v-if="stage === 0" v-model:doc="workflowApiDoc" v-model:goal="workflowRequirements"
      v-model:model="selectedModel" v-model:open-api-url="openApiUrl"
      :generating="workflowLoading" :importing="importing" @generate="startWorkflow" @import="importOpenApi" />
    <ProgressStep v-else-if="stage === 1" :steps="workflowSteps" :running="workflowRunning" @back="resetWorkflow" />
    <ResultStep v-else :project-id="result!.projectId" :title="result!.title" :detail="result!.detail"
      :diagnostic="workflowResult?.diagnostic_summary" :draft-count="workflowResult?.requirements_draft?.items?.length"
      @review="reviewRequirements" @reset="resetWorkflow" />
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter, type HistoryState } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '@/api'
import SourceStep from '@/components/start/SourceStep.vue'
import ProgressStep from '@/components/start/ProgressStep.vue'
import ResultStep from '@/components/start/ResultStep.vue'
import { useI18n } from '@/composables/useI18n'

const router = useRouter()
const { t, currentLang } = useI18n()
const L = (zh: string, en: string) => currentLang.value === 'en' ? en : zh

const selectedModel = ref('')
const stepLabels = computed(() => [L('选择来源', 'Source'), L('生成', 'Generate'), L('核对', 'Review')])

// Workflow state
const workflowApiDoc = ref('')
const workflowRequirements = ref('')
const workflowRunning = ref(false)
const workflowLoading = ref(false)
const workflowSteps = ref<any[]>([])
const workflowResult = ref<any>(null)

const openApiUrl = ref('')
const importing = ref(false)
const importResult = ref<any>(null)

const stage = computed(() => (result.value ? 2 : workflowRunning.value || workflowSteps.value.length ? 1 : 0))
const result = computed(() => {
  if (workflowResult.value?.project_id) {
    return { projectId: workflowResult.value.project_id as string, title: L('项目已生成', 'Project created'), detail: workflowResult.value.message as string }
  }
  if (importResult.value?.success) {
    return {
      projectId: importResult.value.project_id as string,
      title: L('导入完成', 'Import complete'),
      detail: L(`项目「${importResult.value.project_name}」，${importResult.value.endpoints_found} 个接口`,
        `Project "${importResult.value.project_name}", ${importResult.value.endpoints_found} endpoints`),
    }
  }
  return null
})

// Workflow methods
const startWorkflow = async () => {
  if (!workflowApiDoc.value.trim() || !workflowRequirements.value.trim()) {
    ElMessage.warning(t('aiChat.fillRequired'))
    return
  }

  workflowRunning.value = true
  workflowLoading.value = true
  workflowSteps.value = []
  workflowResult.value = null

  try {
    const response = await api.runAIWorkflowStream({
      api_documentation: workflowApiDoc.value,
      testing_requirements: workflowRequirements.value,
      ai_model: selectedModel.value || undefined
    })

    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    const reader = response.body?.getReader()
    if (!reader) throw new Error('No reader')

    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      buffer += decoder.decode(value, { stream: !done })
      const lines = buffer.split(/\r?\n/)
      buffer = lines.pop() || '' // Keep incomplete line in buffer
      if (done && buffer) {
        lines.push(buffer)
        buffer = ''
      }

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          try {
            const data = JSON.parse(line.slice(6))

            // Update progress steps
            const existingIndex = workflowSteps.value.findIndex((s: any) => s.step === data.step)
            const stepObj = {
              step: data.step,
              message: data.message,
              progress: data.progress || 0,
              done: data.step === 'complete' || data.progress >= 100,
              active: data.step !== 'complete' && data.progress < 100 && !data.error,
              error: !!data.error
            }

            if (existingIndex >= 0) {
              workflowSteps.value[existingIndex] = stepObj
            } else {
              // Mark previous steps as done
              workflowSteps.value.forEach((s: any) => {
                if (s.active) s.done = true
                s.active = false
              })
              workflowSteps.value.push(stepObj)
            }

            if (data.step === 'complete') {
              workflowResult.value = {
                message: data.message,
                project_id: data.project_id,
                test_plan_ids: data.test_plan_ids,
                diagnostic_summary: data.diagnostic_summary,
                requirements_draft: data.requirements_draft
              }
              workflowRunning.value = false
            }

            if (data.error) {
              workflowRunning.value = false
              ElMessage.error(data.message)
            }
          } catch { /* skip parse errors */ }
        }
      }
      if (done) {
        if (workflowRunning.value) throw new Error('Workflow stream ended before completion')
        break
      }
    }
  } catch (e: any) {
    ElMessage.error(t('aiChat.workflowFailed', { message: e.message || 'Unknown error' }))
    workflowRunning.value = false
  } finally {
    workflowLoading.value = false
    workflowRunning.value = false
  }
}

async function importOpenApi() {
  if (!openApiUrl.value.trim()) return
  importing.value = true
  try {
    const res = await api.importOpenAPI(openApiUrl.value.trim())
    if (res.data.success) importResult.value = res.data
    else ElMessage.error(res.data.error || L('导入失败', 'Import failed'))
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || L('导入失败', 'Import failed'))
  } finally {
    importing.value = false
  }
}

const resetWorkflow = () => {
  workflowSteps.value = []
  workflowResult.value = null
  workflowRunning.value = false
  importResult.value = null
}

// The requirements tab opens with the drafted requirements to review, and the user's own text
// prefilled in case they want to extract again. history.state takes plain data only.
function reviewRequirements() {
  const doc = workflowResult.value ? `${workflowRequirements.value.trim()}\n\n${workflowApiDoc.value.trim()}`.trim() : ''
  const draft = workflowResult.value?.requirements_draft
  const state: HistoryState = {}
  if (doc) state.requirementsDoc = doc
  if (draft?.items?.length) state.requirementsDraft = JSON.parse(JSON.stringify(draft))
  router.push({ path: `/projects/${result.value!.projectId}/requirements`, state })
}
</script>

<style scoped lang="scss">
.start-page { max-width: 820px; margin: 0 auto; display: flex; flex-direction: column; gap: 16px; }

.intro {
  h2 { margin: 0 0 4px; font-size: 20px; font-weight: 600; color: var(--app-text); }
  p { margin: 0; color: var(--app-text-secondary); }
}

.stepper {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  gap: 24px;
  flex-wrap: wrap;

  li {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--app-text-muted);
    font-size: 13px;
    .index {
      width: 22px;
      height: 22px;
      border-radius: 50%;
      border: 1px solid var(--app-border-strong);
      display: grid;
      place-items: center;
      font-size: 12px;
      font-weight: 600;
    }
    &.active { color: var(--app-text); font-weight: 500; .index { background: var(--app-primary); border-color: var(--app-primary); color: #fff; } }
    &.done .index { background: var(--app-success-soft); border-color: transparent; color: var(--app-success); }
  }
}
</style>
