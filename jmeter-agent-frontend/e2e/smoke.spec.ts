import { expect, test, type Page } from '@playwright/test'

// The whole loop in the browser — document → plan → run → report — against a mocked API,
// so it needs neither a model key nor JMeter. Backend behaviour is covered by pytest.

const project = { id: 'p1', name: 'Shop API', base_url: 'https://shop.example', description: '' }
const plan = { id: 'plan1', project_id: 'p1', name: 'Checkout baseline', test_config: { test_type: 'baseline' },
  endpoints_count: 2, created_at: '2026-10-01T10:00:00' }
const metrics = { total_requests: 1200, avg_response_time: 180, p95_response_time: 250, error_rate: 0, throughput: 40 }
const run = { id: 'run1', project_id: 'p1', project_name: 'Shop API', test_plan_id: 'plan1', test_plan_name: plan.name,
  status: 'completed', start_time: '2026-10-01T10:05:00', end_time: '2026-10-01T10:10:00', progress: 100,
  metrics, requirements: { verdict: 'pass', passed: 1, failed: 0, missing: 0 }, has_ai_analysis: false, has_markdown: false }

const report = {
  execution_id: 'run1', project_id: 'p1', test_plan_id: 'plan1', test_plan_name: plan.name, status: 'completed',
  start_time: run.start_time, end_time: run.end_time,
  summary: { ...metrics, min_response_time: 20, max_response_time: 900, percentiles: { p50: 160, p90: 230, p95: 250, p99: 400 },
    per_label: {}, timeline_segments: [], error_details: [] },
  evidence: {
    execution_id: 'run1', project_id: 'p1', test_plan_id: 'plan1',
    requirements: { verdict: 'pass', passed: 1, failed: 0, missing: 0, items: [{ id: 'R1', transaction: null,
      metric: 'p95', operator: 'lte', expected: 300, actual: 250, status: 'pass', gap_pct: null,
      matched_labels: [], source: 'P95 under 300 ms', source_verified: true }] },
    baseline: { previous: null, baseline: { name: 'September release', verdict: 'regression', summary: '', threshold_pct: 20,
      transactions: [], deltas: [
        { metric: 'p95_response_time', baseline: 200, target: 250, delta_pct: 25, direction: 'worse', is_regression: true },
        { metric: 'throughput', baseline: 38, target: 40, delta_pct: 5.3, direction: 'better', is_regression: false },
      ] } },
    server_metrics: { status: 'not_configured' },
  },
  // Written before the project had requirements, so the report should flag it as out of date.
  ai_analysis: { analysis_status: 'completed', evidence_status: 'unverified', overall_assessment: 'Looks healthy',
    key_findings: [], conclusion: 'Fine', requirements_verdict: { verdict: 'none', passed: 0, failed: 0, missing: 0 } },
}

const workflowStream = [
  { step: 'parse', message: 'Reading the document', progress: 30 },
  { step: 'complete', message: 'Created 1 test plan', progress: 100, project_id: 'p1', test_plan_ids: ['plan1'],
    requirements_draft: { items: [{ id: 'R1', scope: 'transaction', transaction: 'POST /checkout', metric: 'p95',
      operator: 'lte', value: 300, source: 'Checkout P95 within 300 ms', source_verified: true }],
      load_profile: { concurrency: 10, ramp_up_seconds: 10, duration_seconds: 60, target_tps: null, source: '' },
      assumptions: [], source_name: 'start-from-document' } },
].map(event => `data: ${JSON.stringify(event)}\n\n`).join('')

async function mockApi(page: Page) {
  const executed: string[] = []
  await page.route('**/api/v1/**', route => {
    const url = new URL(route.request().url())
    const path = url.pathname.replace('/api/v1', '')
    const json = (body: unknown) => route.fulfill({ json: body })
    if (path === '/ai/workflow/stream') return route.fulfill({ contentType: 'text/event-stream', body: workflowStream })
    if (path === '/ai/models') return json([{ id: 'model1', name: 'Test model' }])
    if (path === '/projects') return json([project])
    if (path === '/projects/p1') return json(project)
    if (path === '/projects/p1/test-plans') return json([plan])
    if (path === '/test-plans/plan1/execute') {
      executed.push('plan1')
      return json({ execution_id: 'run1' })
    }
    if (path === '/executions') return json({ executions: executed.length ? [run] : [], total: executed.length })
    if (path === '/executions/run1/report') return json(report)
    if (path === '/system/resources') return json({ cpu_percent: 10, memory_percent: 40 })
    return json(null)
  })
  return executed
}

test('document to report', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('jmeter-agent-lang', 'en'))
  const executed = await mockApi(page)

  await page.goto('/start')
  await page.getByPlaceholder('Or paste the document here').fill('openapi: 3.0.0\npaths:\n  /checkout: {}')
  await page.getByPlaceholder(/Test login and checkout/).fill('Checkout P95 within 300 ms')
  await page.getByRole('button', { name: 'Generate' }).click()
  await expect(page.getByText('Project created')).toBeVisible()
  await expect(page.getByText('AI drafted 1 performance requirements')).toBeVisible()

  // The draft opens for review, unsaved, on the requirements tab.
  await page.getByRole('button', { name: 'Review requirements' }).click()
  await expect(page).toHaveURL(/\/projects\/p1\/requirements$/)
  await expect(page.getByText('AI drafted these requirements from your document')).toBeVisible()
  await expect(page.getByText('Unsaved', { exact: true })).toBeVisible()
  await expect(page.getByPlaceholder('Endpoint (e.g. POST /api/order) or sampler label')).toHaveValue('POST /checkout')

  await page.getByRole('tab', { name: 'Test plans' }).click()
  await expect(page).toHaveURL(/\/projects\/p1\/plans$/)
  await page.getByRole('row', { name: /Checkout baseline/ }).getByRole('button', { name: 'Run' }).click()
  await expect(page).toHaveURL(/\/executions\?projectId=p1$/)
  expect(executed).toEqual(['plan1'])

  await page.getByRole('row', { name: /Checkout baseline/ }).click()
  await expect(page).toHaveURL(/\/executions\/run1$/)
  await expect(page.getByText('All requirements met')).toBeVisible()
  await expect(page.getByText('Compared with baseline「September release」')).toBeVisible()
  await expect(page.locator('.kpi-delta.bad')).toHaveText(/▲ \+25%/)
  await expect(page.locator('.kpi-delta.good')).toHaveText(/▲ \+5\.3%/)

  await page.getByRole('tab', { name: 'AI analysis' }).click()
  await expect(page.getByText('The requirement verdict has changed since this analysis was written')).toBeVisible()
  await expect(page.getByText('When written: no requirements. Now: 1 met · 0 not met · 0 no data.')).toBeVisible()
})

test('report states what was checked and what cannot be judged', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('jmeter-agent-lang', 'en'))
  await mockApi(page)
  const evidence = report.evidence
  await page.route(url => url.pathname.endsWith('/executions/run1/report'), route => route.fulfill({ json: {
    ...report,
    summary: { ...report.summary, error_details: [{ error_type: '200: no order id', count: 3, percentage: 1,
      sample_messages: ['no order id'], labels: { Checkout: 3 } }] },
    evidence: { ...evidence, plan_issues: [{ severity: 'error', code: 'dead_assertion', message: '断言无效' }],
      requirements: { ...evidence.requirements, verdict: 'incomplete',
        load_gaps: [{ field: 'concurrency', expected: 100, actual: 10 }] },
      baseline: { previous: null, baseline: { ...evidence.baseline.baseline, verdict: 'unknown',
        notes: [{ kind: 'load', baseline: 100, target: 10 }] } } },
    ai_analysis: { ...report.ai_analysis, requirements_verdict: null, evidence_status: 'mismatch',
      fact_check: { checked: 2, mismatches: [{ metric: 'p99', claimed: 900, actual: 400 }] } },
  } }))

  await page.goto('/executions/run1')
  await expect(page.getByText('Below the required load, so this is not a pass')).toBeVisible()
  await expect(page.getByText('An assertion checks nothing, so failed requests may count as successes.')).toBeVisible()
  await expect(page.getByText('Concurrency: required 100, reached 10')).toBeVisible()
  await expect(page.getByText(/The runs used different load \(peak threads 100 → 10\)/)).toBeVisible()
  await page.getByRole('tab', { name: 'Errors 1' }).click()
  await expect(page.getByText('Checkout (3)')).toBeVisible()
  await page.getByRole('tab', { name: 'AI analysis' }).click()
  await expect(page.getByText('Some numbers in the AI text do not match the measurements')).toBeVisible()
  await expect(page.getByText('p99: 900 → 400')).toBeVisible()
})

test('a report can be questioned and keeps the answer', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('jmeter-agent-lang', 'en'))
  await mockApi(page)
  const asked: unknown[] = []
  await page.route(url => url.pathname.endsWith('/executions/run1/questions/stream'), route => {
    asked.push(route.request().postDataJSON())
    const answer = { question: 'Which request was slowest?', answer: 'POST /checkout, at 900 ms.',
      asked_at: '2026-10-01T10:20:00', mismatches: [{ metric: 'p99', claimed: 900, actual: 400 }] }
    const events = [{ step: 'progress', message: 'Checking the numbers in the answer' }, { step: 'answer', answer }]
    return route.fulfill({ contentType: 'text/event-stream',
      body: events.map(event => `data: ${JSON.stringify(event)}\n\n`).join('') })
  })
  await page.route(url => url.pathname.endsWith('/executions/run1/report'), route => route.fulfill({ json: {
    ...report, ai_analysis: { ...report.ai_analysis, follow_ups: [{ question: 'Was it stable?', answer: 'Yes.', mismatches: [] }] },
  } }))

  await page.goto('/executions/run1')
  await page.getByRole('tab', { name: 'AI analysis' }).click()
  await expect(page.getByText('Was it stable?')).toBeVisible()
  const ask = page.getByRole('button', { name: 'Ask', exact: true })
  await expect(ask).toBeDisabled()
  await page.getByPlaceholder(/which request caused most errors/).fill('Which request was slowest?')
  await ask.click()
  await expect(page.getByText('POST /checkout, at 900 ms.')).toBeVisible()
  await expect(page.getByText('Some numbers in this answer do not match the measurements')).toBeVisible()
  await expect(page.getByPlaceholder(/which request caused most errors/)).toHaveValue('')
  expect(asked).toEqual([{ question: 'Which request was slowest?' }])
})

test('a failed run says what stopped it', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('jmeter-agent-lang', 'en'))
  await mockApi(page)
  await page.route(url => url.pathname.endsWith('/executions/run1/report'), route => route.fulfill({ json: {
    ...report, status: 'failed', ai_analysis: null, evidence: null,
    failure_diagnosis: { code: 'missing_plugin', line: 'CannotResolveClassException: kg.apc.UltimateThreadGroup' },
  } }))

  await page.goto('/executions/run1')
  await expect(page.getByText('The plan uses a plugin that is not installed')).toBeVisible()
  await expect(page.getByText('CannotResolveClassException: kg.apc.UltimateThreadGroup')).toBeVisible()
  await page.getByRole('tab', { name: 'AI analysis' }).click()
  await expect(page.getByRole('button', { name: 'Ask', exact: true })).toHaveCount(0)
})

test('an analysis being written appears without a reload', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('jmeter-agent-lang', 'en'))
  await mockApi(page)
  let calls = 0
  await page.route(url => url.pathname.endsWith('/executions/run1/report'), route => {
    calls += 1
    return route.fulfill({ json: calls === 1 ? { ...report, ai_analysis: null, report_status: 'running' } : { ...report, report_status: 'completed' } })
  })

  await page.goto('/executions/run1')
  await page.getByRole('tab', { name: 'AI analysis' }).click()
  await expect(page.getByText('The AI analysis is being written')).toBeVisible()
  await expect(page.getByText('Looks healthy')).toBeVisible({ timeout: 10000 })
})

test('a failed analysis says why and can be written again', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('jmeter-agent-lang', 'en'))
  await mockApi(page)
  let started = false
  await page.route(url => url.pathname.endsWith('/executions/run1/analysis'), route => {
    started = true
    return route.fulfill({ status: 202, json: { report_status: 'running' } })
  })
  await page.route(url => url.pathname.endsWith('/executions/run1/report'), route => route.fulfill({ json: started
    ? { ...report, ai_analysis: null, report_status: 'running' }
    : { ...report, ai_analysis: null, report_status: 'failed', report_error: 'model quota used up' } }))

  await page.goto('/executions/run1')
  await page.getByRole('tab', { name: 'AI analysis' }).click()
  await expect(page.getByText('model quota used up')).toBeVisible()
  await page.getByRole('button', { name: 'Write the AI analysis' }).click()
  await expect(page.getByText('The AI analysis is being written')).toBeVisible()
  expect(started).toBe(true)
})

test('a finding becomes a lesson and a hypothesis is settled by hand', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('jmeter-agent-lang', 'en'))
  await mockApi(page)
  const finding = 'GET /orders slows down first: its P95 reaches 250 ms at 10 users.'
  await page.route(url => url.pathname.endsWith('/executions/run1/report'), route => route.fulfill({ json: {
    ...report, summary: { ...report.summary, per_label: { 'GET /orders': { count: 50 }, 'POST /pay': { count: 20 } } },
    ai_analysis: { ...report.ai_analysis, key_findings: [finding] },
  } }))
  const saved: any[] = []
  const decided: any[] = []
  const lesson = { id: 'l1', kind: 'known_issue', title: 'Orders queue at 10 users', content: finding, labels: ['GET /orders'],
    status: 'active', cited_count: 0, needs_review: false }
  const hypothesis = { id: 'h1', statement: 'The order lookup waits for a lock', category: 'bottleneck', status: 'untested',
    prediction: 'Retry after the stock sync and P95 drops', checkable: false, notes: null }
  await page.route(url => url.pathname.endsWith('/executions/run1/experiences'), route => {
    saved.push(route.request().postDataJSON())
    return route.fulfill({ json: lesson })
  })
  await page.route(url => url.pathname.endsWith('/executions/run1/memory'), route => route.fulfill({ json: {
    labels: ['GET /orders', 'POST /pay'], hypotheses: [hypothesis], experiences: saved.length ? [lesson] : [] } }))
  await page.route(url => url.pathname.endsWith('/hypotheses/h1'), route => {
    decided.push(route.request().postDataJSON())
    return route.fulfill({ json: { ...hypothesis, status: 'refuted', notes: '人工判定：no change after the sync' } })
  })

  await page.goto('/executions/run1')
  await page.getByRole('tab', { name: 'AI analysis' }).click()
  await page.getByRole('button', { name: 'Save as lesson' }).first().click()
  const dialog = page.getByRole('dialog', { name: 'Save as a lesson' })
  await dialog.getByPlaceholder(/One line/).fill('Orders queue at 10 users')
  await expect(dialog.getByText('GET /orders')).toBeVisible()  // The finding names it, so it is preselected.
  await dialog.getByRole('button', { name: 'Save lesson' }).click()
  await expect(dialog).toBeHidden()
  expect(saved).toEqual([{ kind: 'known_issue', title: 'Orders queue at 10 users', content: finding, labels: ['GET /orders'] }])

  await page.getByRole('tab', { name: 'Lessons' }).click()
  await expect(page.getByText('Orders queue at 10 users')).toBeVisible()
  await expect(page.getByText('No program can check this')).toBeVisible()
  await page.getByRole('button', { name: 'It did not hold' }).click()
  const decision = page.getByRole('dialog', { name: 'Mark as not held' })
  await decision.getByRole('textbox').fill('no change after the sync')
  await decision.getByRole('button', { name: 'Save decision' }).click()
  const panel = page.getByRole('tabpanel', { name: 'Lessons' })
  await expect(panel.getByText('Did not hold', { exact: true })).toBeVisible()
  await expect(panel.getByText('Prediction:')).toBeVisible()  // Decided: no longer asks for a decision.
  expect(decided).toEqual([{ status: 'refuted', note: 'no change after the sync' }])
})

test('settings show whether memory is used and a retired lesson can come back', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('jmeter-agent-lang', 'en'))
  await mockApi(page)
  const restored: any[] = []
  await page.route(url => url.pathname.endsWith('/memory/overview'), route => route.fulfill({ json: {
    project_id: 'p1', runs: 15, hypotheses: { untested: 12, confirmed: 2, refuted: 1 }, checkable_open: 4,
    experiences: {}, needs_review: 0, most_cited: [], recently_refuted: [
      { id: 'h9', statement: 'Login is slow because of DNS', notes: 'P95 stayed at 1.2 s with DNS cached' }] } }))
  await page.route(url => url.pathname.endsWith('/projects/p1/experiences'), route => route.fulfill({ json: [
    { id: 'l2', kind: 'module_standard', title: 'Payments under 800 ms', labels: [], status: 'retired', cited_count: 3,
      needs_review: false }] }))
  await page.route(url => url.pathname.endsWith('/experiences/l2'), route => {
    restored.push(route.request().postDataJSON())
    return route.fulfill({ json: { id: 'l2', kind: 'module_standard', title: 'Payments under 800 ms', labels: [],
      status: 'active', cited_count: 3, needs_review: false } })
  })

  await page.goto('/settings?tab=memory')
  await expect(page.getByText('Open hypotheses, 4 checkable')).toBeVisible()
  await expect(page.getByText('Login is slow because of DNS')).toBeVisible()
  await page.getByRole('button', { name: 'Restore' }).click()
  await expect(page.getByRole('button', { name: 'Retire' })).toBeVisible()
  expect(restored).toEqual([{ status: 'active', reason: '' }])
})

test('a model is added from a provider preset, and a local one needs no key', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('jmeter-agent-lang', 'en'))
  await mockApi(page)
  const added: any[] = []
  const preset = (key: string, label: string, group: string, extra = {}) => ({
    key, label, label_en: label, group, api_base: `https://${key}.example/v1`, adapter_type: 'openai_compatible',
    models: [`${key}-model`], key_required: true, base_editable: false, api_version: null, docs: '', note: '',
    note_en: '', ...extra })
  await page.route(url => url.pathname.endsWith('/ai/providers'), route => route.fulfill({ json: [
    preset('deepseek', 'DeepSeek', 'china'), preset('gemini', 'Google Gemini', 'global'),
    preset('ollama', 'Ollama (local)', 'local', { api_base: 'http://127.0.0.1:11434/v1', key_required: false,
      base_editable: true, models: ['qwen3'] }),
  ] }))
  await page.route(url => url.pathname.endsWith('/ai/model-configs'), route => {
    if (route.request().method() === 'POST') {
      added.push(route.request().postDataJSON())
      return route.fulfill({ json: { success: true } })
    }
    return route.fulfill({ json: [] })
  })

  await page.goto('/settings?tab=models')
  await page.getByRole('button', { name: 'Add Model' }).click()
  const dialog = page.getByRole('dialog')
  await dialog.locator('.el-select').first().click()
  await expect(page.getByRole('option', { name: 'Google Gemini' })).toBeVisible()
  await page.getByRole('option', { name: 'Ollama (local)' }).click()
  await expect(dialog.getByPlaceholder('Optional for a server on this machine')).toBeVisible()
  await expect(dialog.getByPlaceholder('http://127.0.0.1:11434/v1')).toHaveValue('http://127.0.0.1:11434/v1')
  await dialog.getByRole('button', { name: 'Add' }).click()
  await expect.poll(() => added.length).toBe(1)
  expect(added[0]).toMatchObject({ provider: 'ollama', model: 'qwen3', api_key: '', api_base: 'http://127.0.0.1:11434/v1' })
})

test('a capacity search shows each step and the load the plan takes', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('jmeter-agent-lang', 'en'))
  await mockApi(page)
  const search = { id: 's1', plan_id: 'plan1', status: 'finished', max_error_rate: 1, capacity_threads: 20,
    capacity_throughput: 400, stop_reason: '40 threads: throughput stopped growing', created_at: '2026-10-01T11:00:00',
    finished_at: '2026-10-01T11:30:00', steps: [
      { threads: 10, execution_id: 'r1', status: 'passed', throughput: 200, p95: 120, error_rate: 0, requirements: 'pass', note: '' },
      { threads: 20, execution_id: 'r2', status: 'passed', throughput: 400, p95: 150, error_rate: 0, requirements: 'pass', note: '' },
      { threads: 40, execution_id: 'r3', status: 'stopped', throughput: 410, p95: 900, error_rate: 0, requirements: 'pass',
        note: 'throughput stopped growing' }] }
  const started: any[] = []
  await page.route(url => url.pathname.endsWith('/test-plans/plan1/capacity-searches'), route => {
    if (route.request().method() === 'POST') {
      started.push(route.request().postDataJSON())
      return route.fulfill({ json: search })
    }
    return route.fulfill({ json: [] })
  })

  await page.goto('/projects/p1/plans')
  await page.getByRole('row', { name: /Checkout baseline/ }).locator('.el-dropdown .el-button').click()
  await page.getByRole('menuitem', { name: 'Capacity search' }).click()
  await page.getByRole('button', { name: 'Start', exact: true }).click()
  await expect(page.getByText('Takes 20 threads at 400.0 requests/s')).toBeVisible()
  await expect(page.getByText('throughput stopped growing', { exact: true })).toBeVisible()
  expect(started[0]).toMatchObject({ factor: 1.5, max_error_rate: 1 })
})
