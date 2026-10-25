<template>
  <div class="projects-page">
    <aside class="project-list">
      <div class="list-toolbar">
        <el-input v-model="keyword" :placeholder="L('搜索项目', 'Search projects')" clearable :prefix-icon="Search" />
        <el-button :title="L('新建空项目', 'New empty project')" @click="openCreate"><el-icon><Plus /></el-icon></el-button>
      </div>
      <div class="list-body" v-loading="loading">
        <button v-for="p in filtered" :key="p.id" :class="['project-item', { active: p.id === projectId }]" @click="select(p.id)">
          <span class="project-name">{{ p.name }}</span>
          <span class="project-count num" :title="L('测试计划数', 'Test plans')">{{ p.test_plan_count || 0 }}</span>
        </button>
        <p v-if="!loading && projects.length && !filtered.length" class="list-empty">{{ L('没有匹配的项目', 'No matching projects') }}</p>
      </div>
    </aside>

    <section class="workspace">
      <div v-if="!loading && !projects.length" class="welcome">
        <h2>{{ L('从一份测试文档开始', 'Start from a test document') }}</h2>
        <p>{{ L('上传需求说明、接口文档或填写 OpenAPI 地址，AI 会生成项目、性能需求草稿和测试计划。', 'Upload a requirements or API document, or give an OpenAPI URL. AI drafts the project, performance requirements and test plans.') }}</p>
        <div class="welcome-actions">
          <el-button type="primary" size="large" @click="router.push('/start')">{{ L('从文档开始', 'Start from a document') }}</el-button>
          <el-button size="large" @click="openCreate">{{ L('新建空项目', 'New empty project') }}</el-button>
        </div>
      </div>

      <template v-else-if="project">
        <header class="workspace-head">
          <div class="title-block">
            <h2>{{ project.name }}</h2>
            <div class="meta">
              <span v-if="project.environment?.base_url" class="mono">{{ project.environment.base_url }}</span>
              <span v-if="project.description" class="desc" :title="project.description">{{ project.description }}</span>
            </div>
          </div>
          <el-button type="primary" @click="router.push(`/projects/${project.id}/test-plans/create`)">
            <el-icon><Plus /></el-icon> {{ L('新建测试计划', 'New test plan') }}
          </el-button>
        </header>

        <el-tabs :model-value="tab" class="workspace-tabs" @tab-change="(name: any) => router.push(`/projects/${project!.id}/${name}`)">
          <el-tab-pane v-for="item in tabs" :key="item.name" :name="item.name" :label="item.label" />
        </el-tabs>

        <ProjectOverview v-if="tab === 'overview'" :project-id="project.id" />
        <RequirementsPanel v-else-if="tab === 'requirements'" :project-id="project.id" :initial-doc="initialDoc" :initial-draft="initialDraft" />
        <PlanList v-else-if="tab === 'plans'" :project-id="project.id" @changed="loadProjects" />
        <BaselineList v-else-if="tab === 'baselines'" :project-id="project.id" />
        <ProjectSettingsPanel v-else :project-id="project.id" @updated="loadProjects" @deleted="onDeleted" />
      </template>
    </section>

    <el-dialog v-model="createVisible" :title="L('新建空项目', 'New empty project')" width="480px" style="max-width: calc(100vw - 32px)" destroy-on-close>
      <el-form label-position="top" @submit.prevent="create">
        <el-form-item :label="L('项目名称', 'Name')" required>
          <el-input v-model="form.name" />
        </el-form-item>
        <el-form-item :label="L('被测系统地址', 'Base URL')">
          <el-input v-model="form.base_url" placeholder="https://api.example.com" />
        </el-form-item>
        <el-form-item :label="L('描述', 'Description')">
          <el-input v-model="form.description" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">{{ L('取消', 'Cancel') }}</el-button>
        <el-button type="primary" :loading="creating" @click="create">{{ L('创建', 'Create') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Plus, Search } from '@element-plus/icons-vue'
import { api } from '@/api'
import { useI18n } from '@/composables/useI18n'
import { PROJECT_TABS } from '@/router'
import ProjectOverview from '@/components/project/ProjectOverview.vue'
import PlanList from '@/components/project/PlanList.vue'
import BaselineList from '@/components/project/BaselineList.vue'
import ProjectSettingsPanel from '@/components/project/ProjectSettingsPanel.vue'
import RequirementsPanel from '@/components/RequirementsPanel.vue'

const LAST_PROJECT_KEY = 'jmeter-agent-last-project'

const route = useRoute()
const router = useRouter()
const { currentLang } = useI18n()
const L = (zh: string, en: string) => currentLang.value === 'en' ? en : zh

const projects = ref<any[]>([])
const loading = ref(true)
const keyword = ref('')
const createVisible = ref(false)
const creating = ref(false)
const form = ref({ name: '', base_url: '', description: '' })

// Handed over by the start-from-document wizard: the user's text, prefilled for extraction,
// and the requirements drafted from it, shown for review until saved.
const initialDoc: string = (window.history.state as any)?.requirementsDoc || ''
const initialDraft = (window.history.state as any)?.requirementsDraft || null
const projectId = computed(() => route.params.projectId as string | undefined)
const project = computed(() => projects.value.find(p => p.id === projectId.value))
const tab = computed(() => (PROJECT_TABS as readonly string[]).includes(route.params.tab as string) ? route.params.tab as string : 'overview')
const tabs = computed(() => [
  { name: 'overview', label: L('概览', 'Overview') },
  { name: 'requirements', label: L('性能需求', 'Requirements') },
  { name: 'plans', label: L('测试计划', 'Test plans') },
  { name: 'baselines', label: L('基线', 'Baselines') },
  { name: 'settings', label: L('设置', 'Settings') },
])
const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return kw ? projects.value.filter(p => p.name.toLowerCase().includes(kw)) : projects.value
})

function readLastProject() {
  try { return localStorage.getItem(LAST_PROJECT_KEY) } catch { return null }
}

async function loadProjects() {
  try {
    const res = await api.getProjects()
    projects.value = res.data?.projects || res.data || []
  } catch {
    projects.value = []
  } finally {
    loading.value = false
  }
}

// Land on a real project: the one in the URL, else the last one opened, else the first.
function ensureSelection() {
  if (loading.value || !projects.value.length || project.value) return
  const last = readLastProject()
  const target = projects.value.find(p => p.id === last) || projects.value[0]
  router.replace(`/projects/${target.id}`)
}

watch([projectId, projects], ensureSelection)
watch(projectId, id => {
  if (!id) return
  try { localStorage.setItem(LAST_PROJECT_KEY, id) } catch { /* storage unavailable */ }
}, { immediate: true })

const select = (id: string) => router.push(`/projects/${id}/${tab.value}`)

function openCreate() {
  form.value = { name: '', base_url: '', description: '' }
  createVisible.value = true
}

async function create() {
  if (!form.value.name.trim()) {
    ElMessage.warning(L('请填写项目名称', 'Enter a project name'))
    return
  }
  creating.value = true
  try {
    const res = await api.createProject({
      name: form.value.name.trim(),
      description: form.value.description.trim() || null,
      environment: { base_url: form.value.base_url.trim(), variables: {}, headers: {} },
    })
    createVisible.value = false
    await loadProjects()
    router.push(`/projects/${res.data?.id || res.data}/requirements`)
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || L('创建失败', 'Create failed'))
  } finally {
    creating.value = false
  }
}

async function onDeleted() {
  await loadProjects()
  router.replace('/projects')
}

onMounted(async () => {
  await loadProjects()
  ensureSelection()
})
</script>

<style scoped lang="scss">
.projects-page {
  display: flex;
  gap: 20px;
  align-items: flex-start;
  max-width: 1440px;
  margin: 0 auto;

  @media (max-width: 900px) { flex-direction: column; align-items: stretch; }
}

.project-list {
  width: 240px;
  flex-shrink: 0;
  position: sticky;
  top: 72px;
  display: flex;
  flex-direction: column;
  gap: 8px;

  @media (max-width: 900px) { width: auto; position: static; }

  .list-toolbar { display: flex; gap: 6px; }
  .list-body { display: flex; flex-direction: column; gap: 2px; max-height: calc(100vh - 140px); overflow-y: auto; min-height: 60px;
    @media (max-width: 900px) { flex-direction: row; overflow-x: auto; max-height: none; }
  }
  .list-empty { color: var(--app-text-muted); font-size: 12px; padding: 8px; }
}

.project-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  height: 34px;
  padding: 0 10px;
  border: none;
  border-radius: var(--app-radius);
  background: transparent;
  color: var(--app-text-secondary);
  font: inherit;
  text-align: left;
  cursor: pointer;
  flex-shrink: 0;

  @media (max-width: 900px) { width: auto; }

  .project-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .project-count { font-size: 12px; color: var(--app-text-muted); }

  &:hover { background: var(--app-hover); color: var(--app-text); }
  &.active { background: var(--app-primary-soft); color: var(--app-primary); font-weight: 500; }
}

.workspace { flex: 1; min-width: 0; }

.workspace-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 4px;

  h2 { margin: 0; font-size: 20px; font-weight: 600; color: var(--app-text); }
  .title-block { flex: 1; min-width: 0; }
  .meta { display: flex; gap: 12px; font-size: 13px; color: var(--app-text-muted); margin-top: 2px; min-width: 0; }
  .meta > * { flex-shrink: 0; }
  .meta .desc { flex-shrink: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mono { font-family: var(--app-font-mono); font-size: 12px; }
}

.workspace-tabs { margin-bottom: 4px; }
.workspace-tabs :deep(.el-tabs__content) { display: none; }

.welcome {
  padding: 64px 24px;
  text-align: center;
  h2 { margin: 0 0 8px; font-size: 22px; color: var(--app-text); }
  p { max-width: 520px; margin: 0 auto 24px; color: var(--app-text-secondary); }
  .welcome-actions { display: flex; gap: 12px; justify-content: center; flex-wrap: wrap; }
}
</style>
