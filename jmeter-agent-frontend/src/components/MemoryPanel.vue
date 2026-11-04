<template>
  <el-card v-loading="loading">
    <template #header>
      <div class="head">
        <span>{{ L('记忆', 'Memory') }}</span>
        <el-select v-model="projectId" filterable style="width: 240px" :placeholder="L('选择项目', 'Choose a project')" @change="load">
          <el-option v-for="p in projects" :key="p.id" :label="p.name" :value="p.id" />
        </el-select>
      </div>
    </template>
    <p class="intro">
      {{ L('报告会读取同一项目的历史运行、待验证的假设和团队保存的经验。这里看它们是否真的在被使用。',
           'Reports read the project\'s earlier runs, open hypotheses and saved lessons. This shows whether they are actually used.') }}
    </p>

    <template v-if="overview">
      <div class="stats">
        <div class="stat"><span class="num">{{ overview.runs }}</span><span>{{ L('记录的运行', 'Runs recorded') }}</span></div>
        <div class="stat"><span class="num">{{ count('untested') }}</span>
          <span>{{ L(`待验证假设，其中 ${overview.checkable_open} 条可自动验证`, `Open hypotheses, ${overview.checkable_open} checkable`) }}</span></div>
        <div class="stat"><span class="num">{{ count('confirmed') }} / {{ count('refuted') }}</span><span>{{ L('预测成立 / 不成立', 'Held / did not hold') }}</span></div>
        <div class="stat"><span class="num">{{ activeLessons }}</span>
          <span>{{ L(`在用的经验，${overview.needs_review} 条需复核`, `Lessons in use, ${overview.needs_review} to review`) }}</span></div>
      </div>

      <el-alert v-if="overview.runs && !activeLessons" type="info" :closable="false" show-icon
        :title="L('这个项目还没有经验', 'This project has no lessons yet')"
        :description="L('在报告的 AI 分析里把确认过的结论“存为经验”，之后的报告就会引用它。', 'Use “Save as lesson” on a conclusion in a report and later reports will cite it.')" />

      <div v-if="overview.recently_refuted.length" class="block">
        <h4>{{ L('最近不成立的预测', 'Predictions that recently did not hold') }}</h4>
        <p v-for="hyp in overview.recently_refuted" :key="hyp.id" class="line">{{ hyp.statement }}<span class="muted"> — {{ hyp.notes }}</span></p>
      </div>
    </template>

    <div class="block">
      <h4>{{ L(`经验 ${lessons.length}`, `Lessons ${lessons.length}`) }}</h4>
      <el-table :data="lessons" size="small" :empty-text="L('还没有经验', 'No lessons yet')">
        <el-table-column :label="L('类型', 'Kind')" width="88"><template #default="{ row }">{{ kindText(row.kind) }}</template></el-table-column>
        <el-table-column :label="L('标题与接口', 'Title and requests')" min-width="180">
          <template #default="{ row }">
            <span :class="{ retired: row.status === 'retired' }">{{ row.title }}</span>
            <el-tag v-if="row.needs_review && row.status === 'active'" size="small" type="warning" class="tag">{{ L('需复核', 'Review') }}</el-tag>
            <div class="labels">{{ row.labels.length ? row.labels.join('、') : L('整个项目', 'Whole project') }}</div>
          </template>
        </el-table-column>
        <el-table-column :label="L('被引用', 'Used')" width="72" align="right"><template #default="{ row }"><span class="num">{{ row.cited_count }}</span></template></el-table-column>
        <el-table-column width="72" align="right">
          <template #default="{ row }">
            <el-button text size="small" :loading="busy === row.id" @click="toggle(row)">
              {{ row.status === 'active' ? L('停用', 'Retire') : L('恢复', 'Restore') }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </el-card>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api'
import { useI18n } from '@/composables/useI18n'

const { L } = useI18n()

const projects = ref<any[]>([])
const projectId = ref('')
const overview = ref<any>(null)
const lessons = ref<any[]>([])
const loading = ref(false)
const busy = ref('')

const count = (status: string) => overview.value?.hypotheses?.[status] ?? 0
const activeLessons = computed(() => lessons.value.filter(row => row.status === 'active').length)
const kindText = (kind: string) => ({
  known_issue: L('已知问题', 'Known issue'), defect_retrospective: L('缺陷复盘', 'Defect review'),
  module_standard: L('接口标准', 'Request standard'),
}[kind] ?? kind)

async function load() {
  if (!projectId.value) return
  loading.value = true
  try {
    const [summary, list] = await Promise.all([api.getMemoryOverview(projectId.value), api.getProjectLessons(projectId.value)])
    overview.value = summary.data
    lessons.value = list.data
  } catch {
    ElMessage.error(L('读取记忆失败', 'Could not load memory'))
  } finally {
    loading.value = false
  }
}

async function toggle(row: any) {
  busy.value = row.id
  try {
    Object.assign(row, (await api.setLessonStatus(row.id, row.status === 'active' ? 'retired' : 'active')).data)
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || L('操作失败', 'That did not work'))
  } finally {
    busy.value = ''
  }
}

onMounted(async () => {
  try {
    const res = await api.getProjects()
    projects.value = res.data?.projects || res.data || []
  } catch {
    projects.value = []
  }
  projectId.value = projects.value[0]?.id || ''
  await load()
})
</script>

<style scoped lang="scss">
.head { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
.intro { margin: 0 0 16px; color: var(--app-text-secondary); line-height: 1.6; }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 16px; }
.stat { display: flex; flex-direction: column; gap: 4px; padding: 12px; border: 1px solid var(--app-border); border-radius: var(--app-radius);
  span:last-child { font-size: 12px; color: var(--app-text-muted); } }
.num { font-size: 20px; font-weight: 600; color: var(--app-text); font-variant-numeric: tabular-nums; }
.stat .num { font-size: 20px; }
.el-table .num { font-size: 13px; font-weight: 400; }
.block { margin-top: 16px; h4 { margin: 0 0 8px; font-size: 14px; } }
.line { margin: 0 0 6px; color: var(--app-text-secondary); }
.muted { color: var(--app-text-muted); }
.retired { color: var(--app-text-muted); text-decoration: line-through; }
.tag { margin-left: 6px; }
.labels { margin-top: 2px; font-size: 12px; color: var(--app-text-muted); overflow-wrap: anywhere; }
</style>
