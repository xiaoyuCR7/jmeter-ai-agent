<template>
  <div class="create-test-plan">
    <el-steps :active="activeStep" finish-status="success" class="steps">
      <el-step :title="t('createPlan.step.template')" />
      <el-step :title="t('createPlan.step.api')" />
      <el-step :title="t('createPlan.step.settings')" />
      <el-step :title="t('createPlan.step.generate')" />
    </el-steps>

    <!-- Step 1: Template Selection -->
    <div v-if="activeStep === 0" class="step-content">
      <h3>{{ t('createPlan.typeTitle') }}</h3>
      <div class="template-grid">
        <el-card
          v-for="template in templates"
          :key="template.id"
          :class="['template-card', { active: selectedTemplate?.id === template.id }]"
          shadow="hover"
          @click="selectTemplate(template)"
        >
          <div class="template-icon">{{ template.icon }}</div>
          <div class="template-name">{{ getTemplateName(template.id) || template.name }}</div>
          <div class="template-desc">{{ getTemplateDesc(template.id) || template.description }}</div>
        </el-card>
      </div>
      <div class="step-actions">
        <el-button type="primary" @click="nextStep" :disabled="!selectedTemplate">{{ t('createPlan.next') }}</el-button>
      </div>
    </div>

    <!-- Step 2: API Configuration -->
    <div v-if="activeStep === 1" class="step-content">
      <h3>{{ t('createPlan.configApi') }}</h3>
      <el-form label-position="top">
        <el-form-item :label="t('createPlan.baseUrl')">
          <el-input v-model="form.base_url" placeholder="https://api.example.com" />
        </el-form-item>

        <el-form-item :label="t('createPlan.apiDoc')">
          <el-input
            v-model="form.api_doc"
            type="textarea"
            :rows="6"
            :placeholder="t('createPlan.apiDocPlaceholder')"
          />
        </el-form-item>

        <el-divider>{{ t('createPlan.orManual') }}</el-divider>

        <div v-for="(endpoint, index) in form.endpoints" :key="index" class="endpoint-card">
          <el-card>
            <template #header>
              <div class="endpoint-header">
                <span>{{ t('createPlan.apiTitle', { number: index + 1 }) }}</span>
                <el-button text type="danger" @click="removeEndpoint(index)">{{ t('createPlan.delete') }}</el-button>
              </div>
            </template>
            <el-row :gutter="12">
              <el-col :span="8" :xs="24" :sm="12">
                <el-form-item :label="t('createPlan.name')">
                  <el-input v-model="endpoint.name" :placeholder="t('createPlan.namePlaceholder')" />
                </el-form-item>
              </el-col>
              <el-col :span="4" :xs="24" :sm="12">
                <el-form-item :label="t('createPlan.method')">
                  <el-select v-model="endpoint.method" style="width: 100%">
                    <el-option label="GET" value="GET" />
                    <el-option label="POST" value="POST" />
                    <el-option label="PUT" value="PUT" />
                    <el-option label="DELETE" value="DELETE" />
                    <el-option label="PATCH" value="PATCH" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :span="12" :xs="24" :sm="24">
                <el-form-item :label="t('createPlan.path')">
                  <el-input v-model="endpoint.path" placeholder="/api/v1/login" />
                </el-form-item>
              </el-col>
            </el-row>

            <el-row :gutter="12">
              <el-col :span="12" :xs="24" :sm="12">
                <el-form-item label="Content-Type">
                  <el-select v-model="endpoint.content_type" style="width: 100%">
                    <el-option label="application/json" value="application/json" />
                    <el-option label="application/x-www-form-urlencoded" value="application/x-www-form-urlencoded" />
                    <el-option label="multipart/form-data" value="multipart/form-data" />
                    <el-option label="text/plain" value="text/plain" />
                    <el-option label="text/xml" value="text/xml" />
                  </el-select>
                </el-form-item>
              </el-col>
              <el-col :span="12" :xs="24" :sm="12">
                <el-form-item :label="t('createPlan.expectedStatus')">
                  <el-input-number v-model="endpoint.expected_status" :min="100" :max="599" style="width: 100%" />
                </el-form-item>
              </el-col>
            </el-row>

            <el-form-item :label="t('createPlan.auth')">
              <el-radio-group v-model="endpoint.authType">
                <el-radio label="none">{{ t('createPlan.auth.none') }}</el-radio>
                <el-radio label="bearer">Bearer Token</el-radio>
                <el-radio label="basic">Basic Auth</el-radio>
              </el-radio-group>
            </el-form-item>
            <el-form-item v-if="endpoint.authType === 'bearer'" label="Token">
              <el-input v-model="endpoint.bearerToken" placeholder="eyJhbGciOiJIUzI1NiIs..." />
            </el-form-item>
            <el-row v-if="endpoint.authType === 'basic'" :gutter="16">
              <el-col :span="12" :xs="24" :sm="12">
                <el-form-item :label="t('createPlan.username')">
                  <el-input v-model="endpoint.basicUser" placeholder="username" />
                </el-form-item>
              </el-col>
              <el-col :span="12" :xs="24" :sm="12">
                <el-form-item :label="t('createPlan.password')">
                  <el-input v-model="endpoint.basicPass" type="password" placeholder="password" show-password />
                </el-form-item>
              </el-col>
            </el-row>

            <el-form-item :label="t('createPlan.headers')">
              <div class="kv-list">
                <div v-for="(h, hIndex) in endpoint.headerList" :key="hIndex" class="kv-row">
                  <el-input v-model="h.key" :placeholder="t('createPlan.headerNamePlaceholder')" />
                  <el-input v-model="h.value" :placeholder="t('createPlan.headerValuePlaceholder')" />
                  <el-button text type="danger" @click="removeHeader(index, hIndex)">
                    <el-icon><Delete /></el-icon>
                  </el-button>
                </div>
                <el-button text type="primary" @click="addHeader(index)">
                  <el-icon><Plus /></el-icon> {{ t('createPlan.addHeader') }}
                </el-button>
              </div>
            </el-form-item>

            <el-form-item :label="t('createPlan.queryParams')">
              <div class="kv-list">
                <div v-for="(q, qIndex) in endpoint.queryList" :key="qIndex" class="kv-row">
                  <el-input v-model="q.key" :placeholder="t('createPlan.paramNamePlaceholder')" />
                  <el-input v-model="q.value" :placeholder="t('createPlan.paramValuePlaceholder')" />
                  <el-button text type="danger" @click="removeQuery(index, qIndex)">
                    <el-icon><Delete /></el-icon>
                  </el-button>
                </div>
                <el-button text type="primary" @click="addQuery(index)">
                  <el-icon><Plus /></el-icon> {{ t('createPlan.addParam') }}
                </el-button>
              </div>
            </el-form-item>

            <el-form-item :label="t('createPlan.requestBody')">
              <el-input
                v-model="endpoint.bodyStr"
                type="textarea"
                :rows="3"
                placeholder='{"username": "test", "password": "123456"}'
              />
            </el-form-item>
          </el-card>
        </div>

        <el-button text type="primary" @click="addEndpoint">
          <el-icon><Plus /></el-icon> {{ t('createPlan.addApi') }}
        </el-button>
      </el-form>

      <div class="step-actions">
        <el-button @click="prevStep">{{ t('createPlan.prev') }}</el-button>
        <el-button type="primary" @click="nextStep">{{ t('createPlan.next') }}</el-button>
      </div>
    </div>

    <!-- Step 3: Test Configuration -->
    <div v-if="activeStep === 2" class="step-content">
      <h3>{{ t('createPlan.testConfig') }}</h3>
      <el-alert v-if="profileApplied" type="success" :closable="false" show-icon style="margin-bottom: 12px"
        :title="currentLang === 'en' ? 'Prefilled from the load profile in the project performance requirements.' : '已按项目性能需求中的负载模型预填并发与时长。'" />
      <el-form :model="form.test_config" label-position="top">
        <el-row :gutter="12">
          <el-col :span="8" :xs="24" :sm="12" :md="8">
            <el-form-item :label="t('createPlan.concurrency')">
              <el-input-number v-model="form.test_config.concurrent_users" :min="1" :max="10000" style="width: 100%;" />
            </el-form-item>
          </el-col>
          <el-col :span="8" :xs="24" :sm="12" :md="8">
            <el-form-item :label="t('createPlan.rampUp')">
              <el-input-number v-model="form.test_config.ramp_up_seconds" :min="1" :max="3600" style="width: 100%;" />
            </el-form-item>
          </el-col>
          <el-col :span="8" :xs="24" :sm="24" :md="8">
            <el-form-item :label="t('createPlan.duration')">
              <el-input-number v-model="form.test_config.duration_seconds" :min="10" :max="86400" style="width: 100%;" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-row :gutter="12">
          <el-col :span="8" :xs="24" :sm="12" :md="8">
            <el-form-item :label="t('createPlan.targetRps')">
              <el-input-number v-model="form.test_config.target_rps" :min="1" :value-on-clear="null"
                :placeholder="t('createPlan.targetRpsUnlimited')" style="width: 100%;" />
            </el-form-item>
          </el-col>
          <el-col :span="8" :xs="24" :sm="12" :md="8">
            <el-form-item :label="t('createPlan.thinkTime')">
              <el-input-number v-model="form.test_config.think_time_ms" :min="0" style="width: 100%;" />
            </el-form-item>
          </el-col>
          <el-col :span="8" :xs="24" :sm="24" :md="8">
            <el-form-item :label="t('createPlan.iterations')">
              <el-input-number v-model="form.test_config.iterations" :min="0" style="width: 100%;" />
            </el-form-item>
          </el-col>
        </el-row>

        <el-form-item :label="t('createPlan.selectModel')">
          <el-select v-model="form.ai_model" :placeholder="t('createPlan.selectModelPlaceholder')" clearable style="width: 100%">
            <el-option
              v-for="config in aiModels"
              :key="config.id"
              :label="config.model"
              :value="config.id"
            />
          </el-select>
        </el-form-item>
      </el-form>

      <div class="step-actions">
        <el-button @click="prevStep">{{ t('createPlan.prev') }}</el-button>
        <el-button type="primary" @click="nextStep">{{ t('createPlan.next') }}</el-button>
      </div>
    </div>

    <!-- Step 4: Generate -->
    <div v-if="activeStep === 3" class="step-content">
      <div v-if="generating" class="generating">
        <el-skeleton :rows="5" animated />
        <p class="generating-text">{{ t('createPlan.generating') }}</p>
      </div>

      <div v-else-if="generatedPlan" class="generated">
        <el-result icon="success" :title="t('createPlan.success')">
          <template #sub-title>
            <p>{{ t('createPlan.planId', { id: generatedPlan.id }) }}</p>
            <p>{{ t('createPlan.apiCount', { count: generatedPlan.endpoints_count }) }}</p>
          </template>
          <template #extra>
            <el-button type="primary" @click="executePlan">{{ t('createPlan.runNow') }}</el-button>
            <el-button @click="router.push(projectId ? `/projects/${projectId}/plans` : '/projects')">{{ t('createPlan.backToList') }}</el-button>
          </template>
        </el-result>
      </div>

      <div v-else class="step-actions">
        <el-button @click="prevStep">{{ t('createPlan.prev') }}</el-button>
        <el-button type="primary" @click="generatePlan" :loading="generating">{{ t('createPlan.generate') }}</el-button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Plus, Delete } from '@element-plus/icons-vue'
import { api } from '@/api'
import { useI18n } from '@/composables/useI18n'

const router = useRouter()
const route = useRoute()
const { t, currentLang } = useI18n()

const getTemplateName = (id: string) => t(`createPlan.template.${id}.name`) || null
const getTemplateDesc = (id: string) => t(`createPlan.template.${id}.desc`) || null

const projectId = computed(() => route.params.projectId as string)
const activeStep = ref(0)
const loading = ref(false)
const generating = ref(false)
const templates = ref<any[]>([])
const selectedTemplate = ref<any>(null)
const aiModels = ref<any[]>([])
const generatedPlan = ref<any>(null)

const form = ref({
  base_url: '',
  api_doc: '',
  endpoints: [] as any[],
  test_config: {
    test_type: '',
    concurrent_users: 100,
    ramp_up_seconds: 60,
    duration_seconds: 300,
    target_rps: null as number | null,
    think_time_ms: 0,
    iterations: null as number | null,
    step_users: 10,
    step_duration: 60,
    stability_hours: 1
  },
  ai_model: null as string | null
})

const loadTemplates = async () => {
  const res = await api.getTemplates()
  templates.value = res.data
}

const loadModels = async () => {
  try {
    const res = await api.getModelConfigs()
    aiModels.value = res.data.filter((config: any) => !config.is_builtin && config.enabled !== false)
  } catch {
    aiModels.value = []
  }
}

const selectTemplate = (template: any) => {
  selectedTemplate.value = template
  form.value.test_config = { ...template.default_config }
}

const addEndpoint = () => {
  form.value.endpoints.push({
    name: '',
    method: 'GET',
    path: '',
    headers: {},
    query_params: {},
    bodyStr: '',
    content_type: 'application/json',
    expected_status: 200,
    authType: 'none',
    bearerToken: '',
    basicUser: '',
    basicPass: '',
    headerList: [],
    queryList: []
  })
}

const removeEndpoint = (index: number) => {
  form.value.endpoints.splice(index, 1)
}

const addHeader = (endpointIndex: number) => {
  form.value.endpoints[endpointIndex].headerList.push({ key: '', value: '' })
}

const removeHeader = (endpointIndex: number, headerIndex: number) => {
  form.value.endpoints[endpointIndex].headerList.splice(headerIndex, 1)
}

const addQuery = (endpointIndex: number) => {
  form.value.endpoints[endpointIndex].queryList.push({ key: '', value: '' })
}

const removeQuery = (endpointIndex: number, queryIndex: number) => {
  form.value.endpoints[endpointIndex].queryList.splice(queryIndex, 1)
}

const buildEndpointPayload = (e: any) => {
  const headers: Record<string, string> = {}
  for (const h of e.headerList || []) {
    if (h.key) headers[h.key] = h.value
  }
  if (e.content_type) {
    headers['Content-Type'] = e.content_type
  }
  if (e.authType === 'bearer' && e.bearerToken) {
    headers['Authorization'] = `Bearer ${e.bearerToken}`
  }
  if (e.authType === 'basic' && e.basicUser) {
    const encoded = typeof window !== 'undefined'
      ? window.btoa(`${e.basicUser}:${e.basicPass || ''}`)
      : Buffer.from(`${e.basicUser}:${e.basicPass || ''}`).toString('base64')
    headers['Authorization'] = `Basic ${encoded}`
  }

  const query_params: Record<string, any> = {}
  for (const q of e.queryList || []) {
    if (q.key) query_params[q.key] = q.value
  }

  let body = undefined
  if (e.bodyStr) {
    try {
      body = JSON.parse(e.bodyStr)
    } catch {
      body = e.bodyStr
    }
  }

  return {
    name: e.name,
    method: e.method,
    path: e.path,
    headers,
    query_params,
    body,
    content_type: e.content_type,
    expected_status: e.expected_status
  }
}

const nextStep = () => {
  if (activeStep.value < 3) {
    activeStep.value++
  }
}

const prevStep = () => {
  if (activeStep.value > 0) {
    activeStep.value--
  }
}

const generatePlan = async () => {
  generating.value = true
  try {
    const payload = {
      api_doc: form.value.api_doc,
      base_url: form.value.base_url,
      test_config: form.value.test_config,
      ai_model: form.value.ai_model,
      endpoints: form.value.endpoints.map(buildEndpointPayload)
    }

    // Use project-scoped API if projectId is available
    let res
    if (projectId.value) {
      res = await api.generateTestPlanInProject(projectId.value, payload)
    } else {
      res = await api.generateTestPlan(payload)
    }
    generatedPlan.value = res.data
    ElMessage.success(t('createPlan.message.success'))
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || t('createPlan.message.failed'))
  } finally {
    generating.value = false
  }
}

const executePlan = async () => {
  if (!generatedPlan.value) return
  try {
    await api.executeTest(generatedPlan.value.id, form.value.ai_model || undefined)
    ElMessage.success(t('createPlan.message.runStarted'))
    router.push(projectId.value ? `/executions?projectId=${projectId.value}` : '/executions')
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || t('createPlan.message.runFailed'))
  }
}

onMounted(async () => {
  await loadTemplates()
  loadModels()

  // Load project data if projectId is in route params
  if (projectId.value) {
    try {
      const projectRes = await api.getProject(projectId.value)
      const project = projectRes.data
      if (project?.environment?.base_url) {
        form.value.base_url = project.environment.base_url
      }
    } catch { /* project may not exist yet */ }
  }

  const templateId = route.query.template as string
  if (templateId) {
    const template = templates.value.find((t: any) => t.id === templateId)
    if (template) {
      selectTemplate(template)
    }
  }
  await applyRequirementLoadProfile()
})

// The project's documented load model drives the plan, so tests measure what the requirements specify.
const profileApplied = ref(false)
async function applyRequirementLoadProfile() {
  if (!projectId.value) return
  try {
    const profile = (await api.getRequirements(projectId.value)).data?.load_profile
    if (!profile) return
    const config = form.value.test_config as any
    const mapping: [keyof typeof profile, string][] = [
      ['concurrency', 'concurrent_users'], ['ramp_up_seconds', 'ramp_up_seconds'],
      ['duration_seconds', 'duration_seconds'], ['target_tps', 'target_rps'],
    ]
    for (const [from, to] of mapping) {
      if (typeof profile[from] === 'number') {
        config[to] = profile[from]
        profileApplied.value = true
      }
    }
  } catch { /* requirements are optional */ }
}
</script>

<style scoped lang="scss">
.create-test-plan {
  .steps {
    margin-bottom: 24px;
    padding: 0 16px;

    @media (min-width: 769px) {
      margin-bottom: 32px;
      padding: 0 40px;
    }
  }

  .step-content {
    max-width: 900px;
    margin: 0 auto;
    padding: 0 16px;

    @media (min-width: 769px) {
      padding: 0;
    }

    h3 {
      margin-bottom: 20px;
      font-size: 18px;
      font-weight: 500;
      color: var(--app-text);

      @media (min-width: 769px) {
        margin-bottom: 24px;
        font-size: 20px;
      }
    }
  }

  .template-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(260px, 100%), 1fr));
    gap: 14px;
    margin-bottom: 24px;

    .template-card {
      cursor: pointer;
      transition: all 0.2s ease;
      text-align: center;
      padding: 22px 16px;
      background: var(--app-surface);
      border: 1px solid var(--app-border);
      border-radius: var(--app-radius-lg);

      &:hover, &.active {
        border-color: var(--app-border-strong);
        box-shadow: var(--app-shadow);
        transform: translateY(-1px);
      }

      &.active {
        background: var(--app-primary-soft);
        border-color: var(--app-primary);
      }

      .template-icon {
        font-size: 36px;
        margin-bottom: 12px;
      }

      .template-name {
        font-size: 15px;
        font-weight: 500;
        margin-bottom: 6px;
        color: var(--app-text);
        overflow-wrap: break-word;
        word-break: normal;
      }

      .template-desc {
        font-size: 13px;
        color: var(--app-text-secondary);
        line-height: 1.4;
        overflow-wrap: break-word;
        word-break: normal;
      }
    }
  }

  .endpoint-card {
    margin-bottom: 16px;

    :deep(.el-card) {
      background: var(--app-surface);
      border: 1px solid var(--app-border);
      border-radius: var(--app-radius-lg);
    }

    .endpoint-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
      font-weight: 500;
      color: var(--app-text);
    }
  }

  .kv-list {
    width: 100%;

    .kv-row {
      display: flex;
      gap: 8px;
      align-items: center;
      margin-bottom: 8px;
      flex-wrap: wrap;

      .el-input {
        flex: 1;
        min-width: 0;
      }

      .el-button {
        flex-shrink: 0;
        width: auto;
      }

      @media (max-width: 768px) {
        .el-input {
          min-width: 120px;
        }
      }
    }
  }

  .step-actions {
    display: flex;
    justify-content: flex-end;
    flex-wrap: wrap;
    gap: 10px;
    margin-top: 24px;
    padding-top: 24px;
    border-top: 1px solid var(--app-border);

    @media (max-width: 768px) {
      justify-content: stretch;

      .el-button {
        flex: 1;
        min-width: 120px;
        width: auto;
      }
    }
  }

  .generating {
    text-align: center;
    padding: 40px;

    .generating-text {
      margin-top: 20px;
      color: var(--app-text-secondary);
      font-size: 14px;
    }
  }

  .generated {
    padding: 40px;
  }
}
</style>
