<template>
  <el-dialog v-model="visible" :title="L('存为经验', 'Save as a lesson')" width="560px" append-to-body>
    <p class="hint">{{ L('同一项目以后的报告和追问会读到它，直到有人停用。', 'Later reports and questions in this project will read it until someone retires it.') }}</p>
    <el-form label-position="top" @submit.prevent>
      <el-form-item :label="L('类型', 'Kind')">
        <el-radio-group v-model="form.kind">
          <el-radio-button v-for="kind in kinds" :key="kind.value" :value="kind.value">{{ kind.label }}</el-radio-button>
        </el-radio-group>
      </el-form-item>
      <el-form-item :label="L('标题', 'Title')">
        <el-input v-model="form.title" maxlength="120" show-word-limit
          :placeholder="L('一句话说清是什么，同名的经验会被更新而不是重复保存', 'One line; a lesson with the same title is updated, not duplicated')" />
      </el-form-item>
      <el-form-item :label="L('内容', 'Content')">
        <el-input v-model="form.content" type="textarea" :autosize="{ minRows: 4, maxRows: 10 }" maxlength="4000" show-word-limit
          :placeholder="L('现象、原因、怎么验证或该遵守的标准，至少 20 个字', 'What happens, why, how to check it or the standard to keep; at least 20 characters')" />
      </el-form-item>
      <el-form-item :label="L('涉及的接口', 'Requests it is about')">
        <el-select v-model="form.labels" multiple filterable clearable style="width: 100%"
          :placeholder="L('不选表示适用于整个项目', 'Leave empty for the whole project')">
          <el-option v-for="label in labels" :key="label" :label="label" :value="label" />
        </el-select>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="visible = false">{{ L('取消', 'Cancel') }}</el-button>
      <el-button type="primary" :loading="saving" :disabled="!canSave" @click="save">{{ L('保存经验', 'Save lesson') }}</el-button>
    </template>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, type LessonKind } from '@/api'
import { useI18n } from '@/composables/useI18n'

const props = defineProps<{ executionId: string; labels: string[] }>()
const emit = defineEmits<{ saved: [lesson: any] }>()

const { currentLang } = useI18n()
const L = (zh: string, en: string) => currentLang.value === 'en' ? en : zh

const kinds = computed(() => [
  { value: 'known_issue' as LessonKind, label: L('已知问题', 'Known issue') },
  { value: 'defect_retrospective' as LessonKind, label: L('缺陷复盘', 'Defect review') },
  { value: 'module_standard' as LessonKind, label: L('接口标准', 'Request standard') },
])
const visible = ref(false)
const saving = ref(false)
const form = reactive({ kind: 'known_issue' as LessonKind, title: '', content: '', labels: [] as string[] })
const canSave = computed(() => form.title.trim().length > 0 && form.content.trim().length >= 20)

function open(content = '', labels: string[] = []) {
  Object.assign(form, { kind: 'known_issue', title: '', content, labels: labels.filter(label => props.labels.includes(label)) })
  visible.value = true
}

async function save() {
  saving.value = true
  try {
    const lesson = (await api.saveLesson(props.executionId, { ...form, title: form.title.trim(), content: form.content.trim() })).data
    emit('saved', lesson)
    visible.value = false
    ElMessage.success(L('已保存，之后的报告会用到它', 'Saved; later reports will use it'))
  } catch (e: any) {
    const detail = e.response?.data?.detail
    ElMessage.error(typeof detail === 'string' ? detail : L('保存失败，内容至少 20 个字', 'Could not save; the content needs at least 20 characters'))
  } finally {
    saving.value = false
  }
}

defineExpose({ open })
</script>

<style scoped lang="scss">
.hint { margin: 0 0 12px; font-size: 13px; color: var(--app-text-muted); }
</style>
