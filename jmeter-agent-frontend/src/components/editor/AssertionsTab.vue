<template>
  <div class="assertions-tab">
    <div class="tab-header">
      <span class="tab-title">{{ t('editor.assertion.title') }}</span>
      <span class="tab-desc">{{ t('editor.assertion.desc') }}</span>
    </div>

    <div v-if="modelValue.length === 0" class="empty-state">
      <el-empty :description="t('editor.assertion.empty')" :image-size="48">
        <el-button type="primary" size="small" @click="addAssertion">{{ t('editor.assertion.add') }}</el-button>
      </el-empty>
    </div>

    <div v-else class="assertions-list">
      <div
        v-for="(assertion, index) in modelValue"
        :key="index"
        class="assertion-card"
      >
        <div class="assertion-header">
          <span class="assertion-index">{{ t('editor.assertion.index', { idx: index + 1 }) }}</span>
          <el-button text size="small" type="danger" @click="removeAssertion(index)">
            <el-icon><Delete /></el-icon>
          </el-button>
        </div>

        <el-form label-position="top" size="small">
          <el-row :gutter="12">
            <el-col :span="8">
              <el-form-item :label="t('editor.assertion.type')">
                <el-select v-model="assertion.type" style="width: 100%">
                  <el-option :label="t('editor.assertion.type.statusCode')" value="status_code" />
                  <el-option :label="t('editor.assertion.type.responseTime')" value="response_time" />
                  <el-option :label="t('editor.assertion.type.bodyContains')" value="body_contains" />
                  <el-option :label="t('editor.assertion.type.bodyRegex')" value="body_regex" />
                  <el-option label="JSON Path" value="json_path" />
                  <el-option :label="t('editor.assertion.type.header')" value="header" />
                  <el-option :label="t('editor.assertion.type.duration')" value="duration" />
                  <el-option :label="t('editor.assertion.type.size')" value="size" />
                  <el-option :label="t('editor.assertion.type.jsr223')" value="jsr223" />
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item :label="t('editor.assertion.operator')">
                  <el-select v-model="assertion.operator" style="width: 100%">
                    <template v-if="assertion.type === 'response_time' || assertion.type === 'duration'">
                      <el-option :label="t('editor.assertion.op.lt')" value="lt" />
                      <el-option :label="t('editor.assertion.op.lte')" value="lte" />
                      <el-option :label="t('editor.assertion.op.gt')" value="gt" />
                      <el-option :label="t('editor.assertion.op.gte')" value="gte" />
                    </template>
                    <template v-else-if="assertion.type === 'size'">
                      <el-option :label="t('editor.assertion.op.eq')" value="eq" />
                      <el-option :label="t('editor.assertion.op.neq')" value="neq" />
                      <el-option :label="t('editor.assertion.op.gt')" value="gt" />
                      <el-option :label="t('editor.assertion.op.lt')" value="lt" />
                      <el-option :label="t('editor.assertion.op.gte')" value="gte" />
                      <el-option :label="t('editor.assertion.op.lte')" value="lte" />
                    </template>
                    <template v-else-if="assertion.type === 'jsr223'">
                      <el-option :label="t('editor.assertion.op.custom')" value="custom" />
                    </template>
                    <template v-else>
                      <el-option :label="t('editor.assertion.op.eq')" value="eq" />
                      <el-option :label="t('editor.assertion.op.neq')" value="neq" />
                      <el-option :label="t('editor.assertion.op.contains')" value="contains" />
                      <el-option :label="t('editor.assertion.op.notContains')" value="not_contains" />
                      <el-option :label="t('editor.assertion.op.regex')" value="regex" />
                    </template>
                  </el-select>
                </el-form-item>
            </el-col>
            <!-- JSON path and header checks edit their path and expected value in the row below. -->
            <el-col v-if="assertion.type !== 'json_path' && assertion.type !== 'header'" :span="8">
              <el-form-item :label="assertion.type === 'duration' ? t('editor.assertion.maxDuration') : assertion.type === 'size' ? t('editor.assertion.sizeBytes') : assertion.type === 'jsr223' ? t('editor.assertion.script') : t('editor.assertion.expected')">
                <template v-if="assertion.type === 'jsr223'">
                  <el-input
                    v-model="assertion.expected"
                    type="textarea"
                    :rows="6"
                    class="script-textarea"
                    :placeholder="t('editor.assertion.jsr223Placeholder')"
                  />
                </template>
                <template v-else>
                  <el-input v-model="assertion.expected" :placeholder="getPlaceholder(assertion.type)" />
                </template>
              </el-form-item>
            </el-col>
          </el-row>

          <el-row v-if="assertion.type === 'json_path' || assertion.type === 'header'" :gutter="12">
            <el-col :span="12">
              <el-form-item :label="assertion.type === 'json_path' ? t('editor.assertion.jsonPathExpr') : t('editor.assertion.headerName')">
                <el-input v-model="assertion.field" :placeholder="assertion.type === 'json_path' ? '$.data.code' : 'Content-Type'" />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item :label="t('editor.assertion.expected')">
                <el-input v-model="assertion.expected" placeholder="200" />
              </el-form-item>
            </el-col>
          </el-row>

          <el-row v-if="assertion.type === 'jsr223'" :gutter="12">
            <el-col :span="8">
              <el-form-item :label="t('editor.scriptLanguage')">
                <el-select v-model="assertion.language" style="width: 100%">
                  <el-option label="Groovy" value="groovy" />
                  <el-option label="JavaScript" value="javascript" />
                  <el-option label="BeanShell" value="beanshell" />
                  <el-option label="Java" value="java" />
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item :label="t('editor.assertion.parameters')">
                <el-input v-model="assertion.parameters" placeholder="参数" />
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item :label="t('editor.assertion.filename')">
                <el-input v-model="assertion.filename" placeholder="脚本文件路径" />
              </el-form-item>
            </el-col>
          </el-row>
        </el-form>
      </div>

      <el-button text type="primary" @click="addAssertion" style="margin-top: 8px">
        <el-icon><Plus /></el-icon> {{ t('editor.assertion.add') }}
      </el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Delete, Plus } from '@element-plus/icons-vue'
import { useI18n } from '@/composables/useI18n'

const { t } = useI18n()

export interface AssertionRule {
  type: string
  operator: string
  field: string
  expected: string
  language?: string
  parameters?: string
  filename?: string
}

const props = defineProps<{
  modelValue: AssertionRule[]
}>()

const emit = defineEmits<{
  'update:modelValue': [value: AssertionRule[]]
}>()

function getPlaceholder(type: string): string {
  const map: Record<string, string> = {
    status_code: '200',
    response_time: '1000',
    body_contains: 'success',
    body_regex: '"code":\\s*0',
    json_path: '0',
    header: 'application/json',
    duration: '1000',
    size: '1024',
  }
  return map[type] || t('editor.assertion.expected')
}

function addAssertion() {
  const list = [...props.modelValue]
  list.push({
    type: 'status_code',
    operator: 'eq',
    field: '',
    expected: '200',
  })
  emit('update:modelValue', list)
}

function removeAssertion(index: number) {
  const list = [...props.modelValue]
  list.splice(index, 1)
  emit('update:modelValue', list)
}
</script>

<style scoped lang="scss">
.assertions-tab {
  padding: 4px 0;

  .tab-header {
    margin-bottom: 16px;
    .tab-title {
      font-size: 14px;
      font-weight: 500;
      color: var(--app-text);
    }
    .tab-desc {
      display: block;
      margin-top: 4px;
      font-size: 12px;
      color: var(--app-text-muted);
    }
  }

  .empty-state {
    padding: 24px 0;
  }

  .assertions-list {
    .assertion-card {
      background: var(--app-bg);
      border: 1px solid var(--app-border);
      border-radius: var(--app-radius-lg);
      padding: 12px 16px;
      margin-bottom: 10px;

      .assertion-header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 8px;

        .assertion-index {
          font-size: 13px;
          font-weight: 500;
          color: var(--app-text-secondary);
        }
      }
    }
  }
}
</style>
