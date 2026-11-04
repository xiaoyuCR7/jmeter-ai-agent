<template>
  <section class="panel">
    <div class="panel-head">
      <div>
        <h3>{{ L('性能基线', 'Baselines') }}</h3>
        <p class="hint">{{ L('在报告页点「设为基线」添加。每份新报告会自动和当前基线对比；计划专属基线优先于项目基线。',
          'Add one with "Set as baseline" on a report. New reports are compared with the active baseline; a plan baseline wins over a project-wide one.') }}</p>
      </div>
    </div>

    <el-table :data="baselines" v-loading="loading" :empty-text="L('还没有基线', 'No baselines yet')">
      <el-table-column :label="L('名称', 'Name')" min-width="180">
        <template #default="{ row }">
          <div class="name">{{ row.name }}</div>
          <router-link v-if="row.execution_id" :to="`/executions/${row.execution_id}`" class="link">
            {{ L('来源执行', 'Source run') }} {{ row.execution_id }}
          </router-link>
        </template>
      </el-table-column>
      <el-table-column :label="L('适用范围', 'Scope')" min-width="150">
        <template #default="{ row }">{{ row.test_plan_id ? (planNames[row.test_plan_id] || row.test_plan_id) : L('整个项目', 'Whole project') }}</template>
      </el-table-column>
      <el-table-column label="Avg" width="90" align="right">
        <template #default="{ row }"><span class="num">{{ formatMs(row.metrics_snapshot?.avg_response_time) }}</span></template>
      </el-table-column>
      <el-table-column label="P95" width="90" align="right">
        <template #default="{ row }"><span class="num">{{ formatMs(row.metrics_snapshot?.p95_response_time) }}</span></template>
      </el-table-column>
      <el-table-column :label="L('错误率', 'Errors')" width="90" align="right">
        <template #default="{ row }"><span class="num">{{ formatPct(row.metrics_snapshot?.error_rate) }}</span></template>
      </el-table-column>
      <el-table-column label="TPS" width="80" align="right">
        <template #default="{ row }"><span class="num">{{ formatTps(row.metrics_snapshot?.throughput) }}</span></template>
      </el-table-column>
      <el-table-column :label="L('创建时间', 'Created')" width="170">
        <template #default="{ row }"><span class="num">{{ formatTime(row.created_at) }}</span></template>
      </el-table-column>
      <el-table-column :label="L('状态', 'Status')" width="90">
        <template #default="{ row }">
          <el-tag v-if="row.is_active" type="success" size="small">{{ L('当前', 'Active') }}</el-tag>
          <el-tag v-else type="info" size="small">{{ L('未启用', 'Inactive') }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column width="150" align="right">
        <template #default="{ row }">
          <el-button v-if="!row.is_active" size="small" text type="primary" @click="activate(row)">{{ L('设为当前', 'Activate') }}</el-button>
          <el-button size="small" text type="danger" @click="remove(row)"><el-icon><Delete /></el-icon></el-button>
        </template>
      </el-table-column>
    </el-table>
  </section>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete } from '@element-plus/icons-vue'
import { api } from '@/api'
import { useI18n } from '@/composables/useI18n'
import { formatMs, formatPct, formatTime, formatTps } from '@/utils/format'

const props = defineProps<{ projectId: string }>()
const { L } = useI18n()

const baselines = ref<any[]>([])
const planNames = ref<Record<string, string>>({})
const loading = ref(false)

async function load() {
  const id = props.projectId
  loading.value = true
  try {
    const [res, plans] = await Promise.all([api.getBaselines(id), api.getProjectTestPlans(id).catch(() => ({ data: [] }))])
    if (id !== props.projectId) return
    baselines.value = res.data || []
    planNames.value = Object.fromEntries((plans.data || []).map((p: any) => [p.id, p.name]))
  } catch {
    if (id === props.projectId) baselines.value = []
  } finally {
    loading.value = false
  }
}
watch(() => props.projectId, load, { immediate: true })

async function activate(row: any) {
  try {
    await api.activateBaseline(row.id)
    ElMessage.success(L('已设为当前基线', 'Baseline activated'))
    await load()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || L('操作失败', 'Failed'))
  }
}

async function remove(row: any) {
  try {
    await ElMessageBox.confirm(L(`确定删除基线「${row.name}」吗？`, `Delete baseline "${row.name}"?`), L('删除基线', 'Delete baseline'), { type: 'warning' })
  } catch { return }
  try {
    await api.deleteBaseline(row.id)
    ElMessage.success(L('已删除', 'Deleted'))
    await load()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || L('删除失败', 'Delete failed'))
  }
}
</script>

<style scoped lang="scss">
.name { font-weight: 500; color: var(--app-text); }
.link { font-size: 12px; color: var(--app-primary); text-decoration: none; &:hover { text-decoration: underline; } }
</style>
