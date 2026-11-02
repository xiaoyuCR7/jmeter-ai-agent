// @vitest-environment node
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import ts from 'typescript'
import { expect, it, vi } from 'vitest'

function selected(source: string, names: string[], context: Record<string, any>) {
  const ast = ts.createSourceFile('fixture.ts', source, ts.ScriptTarget.Latest, true)
  const statements = ast.statements.filter(node =>
    (ts.isFunctionDeclaration(node) && node.name && names.includes(node.name.text)) ||
    (ts.isVariableStatement(node) && node.declarationList.declarations.some(d => ts.isIdentifier(d.name) && names.includes(d.name.text))))
  const code = statements.map(node => node.getText(ast).replace(/^export\s+/, '')).join('\n')
  return runInNewContext(ts.transpileModule(code, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText + `\n({${names.join(',')}})`, context)
}

it('OpenAPI import sends the actual route/body contract', async () => {
  const source = readFileSync(new URL('../src/api/index.ts', import.meta.url), 'utf8')
  const instance = { post: vi.fn() }
  const { api } = selected(source, ['api'], { instance })
  api.importOpenAPI('https://fixture.invalid/spec.json', true)
  expect(instance.post).toHaveBeenLastCalledWith('/openapi/import', { url: 'https://fixture.invalid/spec.json', execute: true })
})

it('run questions and re-analysis go to their own routes', () => {
  const source = readFileSync(new URL('../src/api/index.ts', import.meta.url), 'utf8')
  const instance = { post: vi.fn() }
  const { api } = selected(source, ['api'], { instance })
  api.askAboutRun('run/1', 'why?')
  expect(instance.post).toHaveBeenLastCalledWith('/executions/run%2F1/questions', { question: 'why?' }, { timeout: 300000 })
  api.reanalyzeRun('run/1')
  expect(instance.post).toHaveBeenLastCalledWith('/executions/run%2F1/analysis')
})

it('CSV preview maps the response columns and row arrays to table objects', async () => {
  const source = readFileSync(new URL('../src/views/TestCaseEditor.vue', import.meta.url), 'utf8').match(/<script setup lang="ts">([\s\S]*?)<\/script>/)![1]
  const context = { api: { previewCSV: vi.fn().mockResolvedValue({ data: { columns: ['name', 'age'], rows: [['Alice', '3']] } }) },
    csvDatasetConfig: { filePath: 'fixture.csv' }, csvPreviewColumns: { value: [] }, csvPreviewData: { value: [] },
    ElMessage: { error: vi.fn() }, t: (s: string) => s }
  await selected(source, ['previewCSV'], context).previewCSV()
  expect(context.csvPreviewColumns.value).toEqual(['name', 'age'])
  expect(context.csvPreviewData.value).toEqual([{ name: 'Alice', age: '3' }])
})

it.each(['baseline', 'concurrency', 'stress', 'stability', 'custom', 'spike', 'ramp_up'])('ordinary %s defaults retain the RPS request', async kind => {
  const source = readFileSync(new URL('../src/views/CreateTestPlan.vue', import.meta.url), 'utf8').match(/<script setup lang="ts">([\s\S]*?)<\/script>/)![1]
  const config = { test_type: kind, target_rps: 100, step_users: 10, step_duration: 60, duration_seconds: 300 }
  const context = { api: { generateTestPlan: vi.fn().mockResolvedValue({ data: { id: 'generated' } }) },
    form: { value: { test_config: config, endpoints: [] } }, computed: (fn: () => any) => ({ get value() { return fn() } }),
    generating: { value: false }, generatedPlan: { value: null }, projectId: { value: null },
    buildEndpointPayload: (v: any) => v, ElMessage: { warning: vi.fn(), error: vi.fn(), success: vi.fn() }, t: (s: string) => s }
  await selected(source, ['generatePlan'], context).generatePlan()
  expect(context.api.generateTestPlan).toHaveBeenCalledWith(expect.objectContaining({ test_config: config }))
  expect(context.generatedPlan.value).toEqual({ id: 'generated' })
  expect(context.ElMessage.error).not.toHaveBeenCalled()
})
