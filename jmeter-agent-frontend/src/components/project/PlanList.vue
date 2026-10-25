<template>
  <section class="panel">
    <div class="panel-head">
      <div>
        <h3>{{ L('测试计划', 'Test plans') }}</h3>
        <p class="hint">{{ L('新建时会按性能需求里的并发和时长预填', 'New plans are prefilled from the requirements load profile') }}</p>
      </div>
    </div>

    <el-table :data="plans" v-loading="loading" :empty-text="L('还没有测试计划', 'No test plans yet')"
      @row-click="(row: any) => openEditor(row.id)" class="clickable">
      <el-table-column :label="L('名称', 'Name')" min-width="220">
        <template #default="{ row }">
          <div class="name">{{ row.name }}</div>
          <div v-if="row.description" class="desc">{{ row.description }}</div>
        </template>
      </el-table-column>
      <el-table-column :label="L('类型', 'Type')" width="120">
        <template #default="{ row }"><el-tag size="small" type="info">{{ typeText(row.test_config?.test_type) }}</el-tag></template>
      </el-table-column>
      <el-table-column :label="L('接口数', 'Endpoints')" width="90" align="right">
        <template #default="{ row }"><span class="num">{{ row.endpoints_count || 0 }}</span></template>
      </el-table-column>
      <el-table-column :label="L('创建时间', 'Created')" width="170">
        <template #default="{ row }"><span class="num">{{ formatTime(row.created_at) }}</span></template>
      </el-table-column>
      <!-- Pinned so Run stays reachable when a narrow window makes the table scroll. -->
      <el-table-column :label="L('操作', 'Actions')" width="210" align="right" fixed="right">
        <template #default="{ row }">
          <div class="actions" @click.stop>
            <el-button size="small" type="primary" :loading="running === row.id" @click="run(row.id)">
              <el-icon><VideoPlay /></el-icon> {{ L('运行', 'Run') }}
            </el-button>
            <el-button size="small" @click="openEditor(row.id)">{{ L('编辑', 'Edit') }}</el-button>
            <el-dropdown trigger="click" @command="(cmd: string) => cmd === 'download' ? download(row) : remove(row)">
              <el-button size="small" text><el-icon><MoreFilled /></el-icon></el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="download">{{ L('下载 JMX', 'Download JMX') }}</el-dropdown-item>
                  <el-dropdown-item command="delete" divided>{{ L('删除', 'Delete') }}</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </div>
        </template>
      </el-table-column>
    </el-table>
  </section>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { VideoPlay, MoreFilled } from '@element-plus/icons-vue'
import { api } from '@/api'
import { useI18n } from '@/composables/useI18n'
import { formatTime, testTypeText } from '@/utils/format'

const props = defineProps<{ projectId: string }>()
const emit = defineEmits<{ changed: [] }>()
const router = useRouter()
const { currentLang } = useI18n()
const L = (zh: string, en: string) => currentLang.value === 'en' ? en : zh
const typeText = (type?: string) => testTypeText(type, currentLang.value)

const plans = ref<any[]>([])
const loading = ref(false)
const running = ref<string | null>(null)

async function load() {
  const id = props.projectId
  loading.value = true
  try {
    const res = await api.getProjectTestPlans(id)
    if (id === props.projectId) plans.value = res.data || []
  } catch {
    if (id === props.projectId) plans.value = []
  } finally {
    loading.value = false
  }
}
watch(() => props.projectId, load, { immediate: true })

const openEditor = (planId: string) => router.push(`/projects/${props.projectId}/editor/${planId}`)

async function run(planId: string) {
  running.value = planId
  try {
    await api.executeTest(planId)
    ElMessage.success(L('已开始执行', 'Run started'))
    router.push(`/executions?projectId=${props.projectId}`)
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || L('启动执行失败', 'Failed to start the run'))
  } finally {
    running.value = null
  }
}

async function download(plan: any) {
  try {
    const res = await api.downloadTestPlan(plan.id)
    const url = URL.createObjectURL(new Blob([res.data]))
    const a = document.createElement('a')
    a.href = url
    a.download = `${plan.name || 'test_plan_' + plan.id}.jmx`
    a.click()
    setTimeout(() => URL.revokeObjectURL(url), 1000)
  } catch {
    ElMessage.error(L('下载失败', 'Download failed'))
  }
}

async function remove(plan: any) {
  try {
    await ElMessageBox.confirm(L(`确定删除测试计划「${plan.name}」吗？`, `Delete test plan "${plan.name}"?`),
      L('删除测试计划', 'Delete test plan'), { type: 'warning' })
  } catch { return }
  try {
    await api.deleteTestPlan(plan.id)
    ElMessage.success(L('已删除', 'Deleted'))
    await load()
    emit('changed')
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || L('删除失败', 'Delete failed'))
  }
}
</script>

<style scoped lang="scss">
.name { font-weight: 500; color: var(--app-text); }
.desc { font-size: 12px; color: var(--app-text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.actions { display: flex; justify-content: flex-end; align-items: center; gap: 4px; }
.clickable :deep(.el-table__row) { cursor: pointer; }
</style>
