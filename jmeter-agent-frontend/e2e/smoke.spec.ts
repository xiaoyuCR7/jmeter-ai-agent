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
