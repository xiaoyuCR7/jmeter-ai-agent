import axios from 'axios'
import { withScriptApproval } from './scriptApproval'
import { baseURL, requestAccess } from './session'

export type AgentRunRequest = {
  task: string
  context?: string | null
  model?: string | null
  session_id?: string | null
  stream?: boolean
} & ({ agent_type: 'analysis'; execution_id?: string | null }
  | { agent_type?: 'test_plan' | 'workflow'; execution_id?: never })

export interface RegisteredAnalysis {
  execution_id: string
  success: boolean
  error?: string | null
  phase?: 'history' | 'evaluation'
  error_code?: 'ledger_read_failed'
  fact?: Record<string, unknown> | null
  validation?: Record<string, unknown> | null
  structured?: Record<string, unknown> | null
  archive?: {
    fact_archived?: boolean
    hypotheses_archived?: string[]
    conclusion_archived?: string | null
    raw_report_archived?: string | null
    errors?: string[]
    [key: string]: unknown
  } | null
  rag?: { history_found: boolean; historical_facts_count: number; historical_hypotheses_count: number }
}

export interface AgentRunResult {
  agent_type: string
  task: string
  success: boolean
  output: string
  plan_steps: { step_id: number; description: string; success: boolean }[]
  iterations: number
  error: string | null
  duration_seconds: number
  analysis?: RegisteredAnalysis
}

export interface AgentStreamEvent {
  type: string
  message: string
  timestamp: string
  data?: (Record<string, unknown> & { analysis?: RegisteredAnalysis }) | null
}

export type RequirementMetric = 'avg_response_time' | 'p90' | 'p95' | 'p99' | 'max_response_time' | 'error_rate' | 'throughput'
export type RequirementOperator = 'lte' | 'lt' | 'gte' | 'gt'

export interface RequirementItem {
  id: string
  scope: 'global' | 'transaction'
  transaction: string | null
  metric: RequirementMetric
  operator: RequirementOperator
  value: number
  source: string
  source_verified: boolean
}

export interface LoadProfile {
  concurrency: number | null
  ramp_up_seconds: number | null
  duration_seconds: number | null
  target_tps: number | null
  source: string
}

export interface PerformanceRequirements {
  items: RequirementItem[]
  load_profile: LoadProfile
  assumptions: string[]
  source_name: string
  updated_at?: string | null
}

export interface RequirementVerdict {
  id: string
  description: string
  transaction: string | null
  matched_labels: string[]
  metric: RequirementMetric
  operator: RequirementOperator
  expected: number
  actual: number | null
  status: 'pass' | 'fail' | 'missing'
  gap_pct: number | null
  source: string
  source_verified: boolean
}

export interface RequirementEvaluation {
  verdict: 'pass' | 'fail' | 'incomplete' | 'none'
  passed: number
  failed: number
  missing: number
  items: RequirementVerdict[]
  load_profile: LoadProfile | null
}

export interface MetricQuery {
  name: string
  promql: string
  unit: string
  saturation: number | null
}

export interface MonitoringConfig {
  prometheus_url: string
  queries: MetricQuery[]
  step_seconds?: number | null
}

export interface ServerMetricRow {
  name: string
  unit: string
  series?: string
  avg?: number
  max?: number
  p95?: number
  peak_at_s?: number
  saturation?: number | null
  above_saturation_pct?: number | null
  corr_response_time?: number | null
  error?: string
  no_data?: boolean
}

export interface MonitoringTestResult {
  reachable: boolean
  version: string | null
  error: string | null
  queries: { name: string; series: number; error: string | null }[]
}

export interface ServerMetricsEvidence {
  status: 'ok' | 'not_configured' | 'error' | 'no_data'
  error?: string
  collected_at?: string
  prometheus_url?: string
  metrics?: ServerMetricRow[]
  findings?: string[]
  chart?: { name: string; unit: string; series: string; points: [number, number][] }[]
}

export interface HistoryComparison {
  name: string
  execution_id?: string | null
  baseline_id?: number
  verdict: 'stable' | 'regression' | 'improvement' | 'mixed' | 'unknown'
  threshold_pct?: number
  summary: string
  deltas: { metric: string; baseline: number; target: number; delta_pct: number | null; direction: string; is_regression: boolean }[]
  transactions: { label: string; metric: string; metric_title: string; baseline: number; target: number; delta_pct: number | null; is_regression: boolean }[]
}

export interface HistoryEvidence {
  baseline: HistoryComparison | null
  previous: HistoryComparison | null
  is_baseline?: boolean
}

export interface ExecutionEvidence {
  execution_id: string
  project_id: string
  test_plan_id: string | null
  requirements: RequirementEvaluation
  baseline: HistoryEvidence | null
  server_metrics: ServerMetricsEvidence | null
}


const instance = axios.create({
  baseURL,
  timeout: 120000,
})

const MAX_RETRIES = 3
const RETRY_DELAY = 1000
// Only idempotent methods are retried — retrying POST (e.g. execute) would
// trigger duplicate side effects on the server.
const RETRYABLE_METHODS = new Set(['get', 'head', 'options'])

function isRetryableError(error: any): boolean {
  if (!error.response) return true
  const status = error.response.status
  return status === 500 || status === 502 || status === 503 || status === 504 || status === 429
}

function getRetryDelay(retryCount: number): number {
  return RETRY_DELAY * Math.pow(2, retryCount - 1) + Math.random() * 500
}

function currentLang(): 'zh-CN' | 'en' {
  try {
    const saved = localStorage.getItem('jmeter-agent-lang')
    if (saved === 'en' || saved === 'zh-CN') return saved
  } catch { /* localStorage unavailable */ }
  return 'zh-CN'
}

instance.interceptors.request.use((config) => {
  const lang = currentLang()
  config.headers['Accept-Language'] = lang
  if (!config.params) config.params = {}
  if (!config.params.lang) config.params.lang = lang
  return config
})

instance.interceptors.response.use(
  (response) => response,
  async (error) => {
    if (error.response?.data?.code === 'local_access_required') requestAccess()
    const config = error.config
    if (!config) {
      return Promise.reject(error)
    }
    if (!config.retry) {
      config.retry = 0
    }

    const method = (config.method || 'get').toLowerCase()
    if (
      config.retry >= MAX_RETRIES ||
      !RETRYABLE_METHODS.has(method) ||
      !isRetryableError(error)
    ) {
      return Promise.reject(error)
    }

    config.retry += 1
    const delay = getRetryDelay(config.retry)

    console.log(`[API] Retrying request (${config.retry}/${MAX_RETRIES}) in ${Math.round(delay)}ms: ${config.url}`)

    await new Promise((resolve) => setTimeout(resolve, delay))
    return instance(config)
  }
)

export const api = {
  instance,
  getSystemInfo: () => instance.get('/system/info'),
  updatePathsConfig: (data: any) => instance.put('/system/paths', data),

  getModels: () => instance.get('/ai/models'),
  getModelConfigs: () => instance.get('/ai/model-configs'),
  addModelConfig: (data: any) => instance.post('/ai/model-configs', data),
  updateModelConfig: (id: string, data: any) => instance.put(`/ai/model-configs/${id}`, data),
  deleteModelConfig: (id: string) => instance.delete(`/ai/model-configs/${id}`),
  testModelConfig: (modelId: string) => instance.post(`/ai/model-configs/${modelId}/test`),

  getTemplates: () => instance.get('/templates'),

  getProjects: () => instance.get('/projects'),
  getProject: (id: string) => instance.get(`/projects/${id}`),
  createProject: (data: any) => instance.post('/projects', data),
  updateProject: (id: string, data: any) => instance.put(`/projects/${id}`, data),
  deleteProject: (id: string) => instance.delete(`/projects/${id}`),
  getRequirements: (projectId: string) =>
    instance.get<PerformanceRequirements | null>(`/projects/${encodeURIComponent(projectId)}/requirements`),
  saveRequirements: (projectId: string, data: PerformanceRequirements) =>
    instance.put<PerformanceRequirements>(`/projects/${encodeURIComponent(projectId)}/requirements`, data),
  deleteRequirements: (projectId: string) => instance.delete(`/projects/${encodeURIComponent(projectId)}/requirements`),
  extractRequirements: (projectId: string, text: string, sourceName = '', model?: string) =>
    instance.post<PerformanceRequirements>(`/projects/${encodeURIComponent(projectId)}/requirements/extract`,
      { text, source_name: sourceName, model }, { timeout: 300000 }),
  getMonitoringPresets: () => instance.get<Record<string, MetricQuery[]>>('/monitoring/presets'),
  getMonitoring: (projectId: string) => instance.get<MonitoringConfig | null>(`/projects/${encodeURIComponent(projectId)}/monitoring`),
  saveMonitoring: (projectId: string, data: MonitoringConfig) =>
    instance.put<MonitoringConfig>(`/projects/${encodeURIComponent(projectId)}/monitoring`, data),
  deleteMonitoring: (projectId: string) => instance.delete(`/projects/${encodeURIComponent(projectId)}/monitoring`),
  testMonitoring: (projectId: string, data: MonitoringConfig) =>
    instance.post<MonitoringTestResult>(`/projects/${encodeURIComponent(projectId)}/monitoring/test`, data),
  promoteBaseline: (executionId: string) => instance.post(`/executions/${encodeURIComponent(executionId)}/promote-baseline`),
  refreshServerMetrics: (executionId: string) =>
    instance.post<ExecutionEvidence>(`/executions/${encodeURIComponent(executionId)}/server-metrics/refresh`),
  getExecutionEvidence: (executionId: string) => instance.get<ExecutionEvidence>(`/executions/${encodeURIComponent(executionId)}/evidence`),

  generateTestPlan: (data: any) => instance.post('/test-plans/generate', data),
  generateTestPlanInProject: (projectId: string, data: any) =>
    instance.post(`/projects/${projectId}/test-plans/generate`, data),
  getProjectTestPlans: (projectId: string) => instance.get(`/projects/${projectId}/test-plans`),
  deleteTestPlan: (id: string) => instance.delete(`/test-plans/${id}`),
  downloadTestPlan: (id: string) => instance.get(`/test-plans/${id}/download`, { responseType: 'blob' }),
  parseTestPlan: (id: string) => instance.get(`/test-plans/${id}/parse`),
  saveTestPlanTree: (id: string, data: any) => instance.put(`/test-plans/${id}/tree`, data),
  getJmxContent: (id: string) => instance.get(`/test-plans/${id}/jmx/content`),

  getComponents: () => instance.get('/jmeter/components'),
  getComponentChildren: (parent: string) => instance.get(`/jmeter/components/${parent}/children`),
  getFunctions: () => instance.get('/jmeter/functions'),

  executeTest: (planId: string, aiModel?: string) => withScriptApproval(approvedSha256 =>
    instance.post(`/test-plans/${planId}/execute`, null,
      { params: { ai_model: aiModel, approved_sha256: approvedSha256 } })),
  getExecutions: (projectId?: string) => instance.get('/executions', { params: projectId ? { project_id: projectId } : {} }),
  cancelExecution: (id: string) => instance.post(`/executions/${id}/cancel`),

  runAgent: (data: AgentRunRequest) => instance.post<AgentRunResult>('/agent/run', data),

  runAIWorkflowStream: (data: any) => {
    const lang = currentLang()
    return fetch(`${baseURL}/ai/workflow/stream`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Accept-Language': lang },
      body: JSON.stringify({ ...data, lang })
    }).then((response) => {
      if (response.status === 401) requestAccess()
      return response
    })
  },

  getProjectEnvironments: (projectId: string) => instance.get(`/projects/${projectId}/environments`),
  createEnvironment: (projectId: string, data: any) => instance.post(`/projects/${projectId}/environments`, data),
  updateEnvironment: (projectId: string, envId: string, data: any) => instance.put(`/projects/${projectId}/environments/${envId}`, data),
  deleteEnvironment: (projectId: string, envId: string) => instance.delete(`/projects/${projectId}/environments/${envId}`),
  setActiveEnvironment: (projectId: string, envId: string) => instance.put(`/projects/${projectId}/environments/active/${envId}`),

  getReport: (id: string) => instance.get(`/executions/${id}/report`),
  getLog: (id: string, lines?: number) => instance.get(`/executions/${id}/log`, { params: { lines } }),

  downloadAnalysisReport: (executionId: string) => instance.get(`/analysis/reports/${executionId}/download`, { responseType: 'blob' }),


  importOpenAPI: (url: string, execute?: boolean) =>
    instance.post('/openapi/import', { url, execute }),


  checkResources: () => instance.get('/system/resources'),

  getBaselines: (project_id?: string) => instance.get('/baselines', { params: project_id ? { project_id } : {} }),
  createBaseline: ({ thresholds, metrics_snapshot, ...params }: any) =>
    instance.post('/baselines', { thresholds, metrics_snapshot }, { params }),
  activateBaseline: (id: number) => instance.post(`/baselines/${id}/activate`),
  deleteBaseline: (id: number) => instance.delete(`/baselines/${id}`),

  previewCSV: (filepath: string, rows?: number) => instance.get('/data/preview', { params: { filepath, rows } }),

  pendingConfirmations: () => instance.get('/confirmations'),
  decideConfirmation: (id: string, approved: boolean) => instance.post(`/confirmations/${encodeURIComponent(id)}`, { approved }),
}

;(api as any).instance = instance

export default instance
