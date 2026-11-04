// @vitest-environment jsdom
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { resolve } from 'node:path'
import ts from 'typescript'
import { describe, expect, it, vi } from 'vitest'
import * as Vue from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElInputNumber, ElSwitch } from 'element-plus'
import HeadersEditor from '../src/components/editor/HeadersEditor.vue'
import ThreadGroupForm from '../src/components/editor/ThreadGroupForm.vue'
import EndpointSettingsForm from '../src/components/editor/EndpointSettingsForm.vue'
import ParamsEditor from '../src/components/editor/ParamsEditor.vue'
import { usePlanTree } from '../src/composables/usePlanTree'

vi.mock('@/composables/useI18n', () => ({ useI18n: () => ({ t: (key: string) => key, L: (zh: string) => zh }) }))

// Exercise the real setup functions, without importing unrelated UI components.
function editor(path = 'src/views/TestCaseEditor.vue') {
  const file = readFileSync(resolve(path), 'utf8')
  const script = file.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)![1]
  const ast = ts.createSourceFile('setup.ts', script, ts.ScriptTarget.Latest, true)
  const declarations = ast.statements.filter(node => ts.isFunctionDeclaration(node) ||
    (ts.isVariableStatement(node) && node.declarationList.declarations.some(d =>
      ts.isIdentifier(d.name) && ['headerProps'].includes(d.name.text))))
  const source = declarations.map(node => node.getText(ast)).join('\n')
  const js = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  const ref = (value: any) => Vue.ref(value)
  const context = {
    treeData: ref(null), endpoints: ref(new Map()), threadGroupNode: ref(null),
    selectedEndpointId: ref(null), selectedEndpoint: ref(null), mainView: ref('edit'), debugTab: ref('params'),
    planId: ref('plan'), planName: ref('Plan'), saving: ref(false), running: ref(false),
    threadGroupConfig: Vue.reactive({ numThreads: 1, rampTime: 1, loops: 1, duration: 0, delay: 0, scheduler: false, onSampleError: 'continue' }),
    csvDatasetConfig: { filePath: '', variableNames: '', delimiter: ',', encoding: 'UTF-8', ignoreFirstLine: true, recycle: true },
    api: { saveTestPlanTree: vi.fn().mockResolvedValue({}), executeTest: vi.fn().mockResolvedValue({}) },
    router: { push: vi.fn() }, ElMessage: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
    t: (key: string, args?: any) => args?.msg || key,
    computed: Vue.computed,
  }
  // The tree conversion lives in a composable; the view's own functions call it by these names.
  Object.assign(context, usePlanTree(context as any))
  const names = declarations.flatMap(node => ts.isFunctionDeclaration(node) ? [node.name!.text] :
    (node as ts.VariableStatement).declarationList.declarations.map(d => (d.name as ts.Identifier).text))
  const functions = runInNewContext(`${js}\n({${names.join(',')}})`, context)
  return { ...context, ...functions, load(tree: any) { context.treeData.value = tree; functions.parseTreeToEndpoints(tree) } }
}

function mountReal(component: any, props: Record<string, any>) {
  return mount(component, { props, global: { plugins: [ElementPlus], stubs: { User: true, Delete: true, Plus: true } } })
}

// The form item with this label, or undefined when it is not rendered.
function item(wrapper: any, label: string) {
  return wrapper.findAll('.el-form-item').find((w: any) => {
    const text = w.find('.el-form-item__label')
    return text.exists() && text.text() === label
  })
}

describe('real editor control bindings', () => {
  // The form controls live in their own components; each test mounts the real one on the editor's own state.
  const threadGroupForm = (e: any) => mountReal(ThreadGroupForm, { config: e.threadGroupConfig, node: e.threadGroupNode.value,
    numberValue: e.threadGroupNumberValue, canEdit: e.canEditNumber })

  it.each([undefined, 0, '0'])('allows editing duration after explicitly enabling a scheduler with duration %s', async raw => {
    const e = editor(); const original = plan(); const props = find(original, 'group1').properties
    props['ThreadGroup.scheduler'] = false
    if (raw === undefined) delete props['ThreadGroup.duration']
    else props['ThreadGroup.duration'] = raw
    e.load(original)
    const wrapper = threadGroupForm(e)
    expect(e.buildTreeFromEndpoints()).toEqual(original)
    expect(item(wrapper, 'editor.tg.duration')).toBeUndefined()
    await wrapper.findComponent(ElSwitch).trigger('click')
    const input = item(wrapper, 'editor.tg.duration')!.findComponent(ElInputNumber)
    expect(input.exists()).toBe(true)
    await input.find('input').setValue('300')
    await input.find('input').trigger('change')
    expect(find(e.buildTreeFromEndpoints(), 'group1').properties).toMatchObject({ 'ThreadGroup.scheduler': true, 'ThreadGroup.duration': 300 })
    wrapper.unmount()
  })

  it.each([
    ['numThreads', 'ThreadGroup.num_threads', '20000'],
    ['rampTime', 'ThreadGroup.ramp_time', '7200'],
    ['loops', 'loops', '-2'],
    ['duration', 'ThreadGroup.duration', '${duration}'],
    ['duration', 'ThreadGroup.duration', '0'],
    ['delay', 'ThreadGroup.delay', '90000'],
  ])('shows and preserves unsupported original %s=%s (%s)', async (field, property, raw) => {
    const e = editor(); const original = plan(); find(original, 'group1').properties[property] = raw; e.load(original)
    const wrapper = threadGroupForm(e)
    await Vue.nextTick()
    const control = item(wrapper, `editor.tg.${field}`)!
    expect((control.find('input').element as HTMLInputElement).value).toBe(raw)
    expect(control.find('input').attributes('readonly')).toBeDefined()
    expect(control.text()).toContain('JMX')
    expect(e.buildTreeFromEndpoints()).toEqual(original)
    wrapper.unmount()
  })

  it('retains normal number editing and original business bounds', async () => {
    const e = editor(); e.load(plan())
    const wrapper = threadGroupForm(e)
    const control = item(wrapper, 'editor.tg.numThreads')!
    expect(control.findComponent(ElInputNumber).props()).toMatchObject({ min: 1, max: 10000 })
    await control.find('input').setValue('125')
    await control.find('input').trigger('change')
    expect(find(e.buildTreeFromEndpoints(), 'group1').properties['ThreadGroup.num_threads']).toBe(125)
    wrapper.unmount()
  })

  it.each(['${timeout}', '-1', '1.5'])('preserves and shows unsupported sampler timeout %s on mount', async raw => {
    const e = editor(); const original = plan(); find(original, 'request').properties['HTTPSampler.connect_timeout'] = raw
    e.load(original); e.selectedEndpoint.value = e.endpoints.value.get('request')
    const wrapper = mountReal(EndpointSettingsForm, { endpoint: e.selectedEndpoint.value, canEdit: e.canEditNumber })
    await Vue.nextTick()
    const control = item(wrapper, 'editor.connectionTimeout')!
    expect((control.find('input').element as HTMLInputElement).value).toBe(raw)
    expect(control.find('input').attributes('readonly')).toBeDefined()
    expect(e.buildTreeFromEndpoints()).toEqual(original)
    wrapper.unmount()
  })

  it('writes a parameter cell edit back before the unsupported-edit save guard', async () => {
    const e = editor(); e.load(plan()); e.selectedEndpoint.value = e.endpoints.value.get('request')
    const wrapper = mountReal(ParamsEditor, { params: e.selectedEndpoint.value.queryParams })
    await flushPromises()
    await wrapper.find('input[placeholder="editor.paramValue"]').setValue('changed-page')
    expect(e.selectedEndpoint.value.queryParams[0].value).toBe('changed-page')
    expect(await e.handleSave()).toBe(false)
    expect(e.api.saveTestPlanTree).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('adds and removes parameter rows on the endpoint itself', async () => {
    const e = editor(); e.load(plan()); e.selectedEndpoint.value = e.endpoints.value.get('request')
    const wrapper = mountReal(ParamsEditor, { params: e.selectedEndpoint.value.queryParams })
    await wrapper.findAll('button').find((button: any) => button.text().includes('editor.addParam'))!.trigger('click')
    expect(e.selectedEndpoint.value.queryParams).toHaveLength(2)
    await flushPromises()
    await wrapper.findAll('.el-table__body tr')[1].find('button').trigger('click')
    expect(e.selectedEndpoint.value.queryParams.map((p: any) => p.name)).toEqual(['page'])
    wrapper.unmount()
  })

  it('writes a shared header cell edit through TestCaseEditor to its lossless save patch', async () => {
    const e = editor(); e.load(plan()); e.selectedEndpoint.value = e.endpoints.value.get('request')
    const wrapper = mount({ render: () => Vue.h(HeadersEditor, { modelValue: e.headerProps.value,
      'onUpdate:modelValue': (value: any) => { e.headerProps.value = value } }) },
    { global: { plugins: [ElementPlus], stubs: { FunctionHelper: true, Plus: true } } })
    await flushPromises()
    await wrapper.findAll('input')[1].setValue('text/plain')
    expect(e.selectedEndpoint.value.headers[0].value).toBe('text/plain')
    const expected = plan(); find(expected, 'headers').properties['HeaderManager.headers'][0]['Header.value'] = 'text/plain'
    expect(e.buildTreeFromEndpoints()).toEqual(expected)
    expect(wrapper.findComponent(HeadersEditor).emitted('update:modelValue')).toHaveLength(1)
    wrapper.unmount()
  })

  it('emits a complete header update for JmxEditor without dropping unknown properties', async () => {
    const original = find(plan(), 'headers').properties
    const wrapper = mount(HeadersEditor, { props: { modelValue: original },
      global: { plugins: [ElementPlus], stubs: { FunctionHelper: true, Plus: true } } })
    await flushPromises()
    await wrapper.findAll('input')[0].setValue('Content-Type')
    const expected = JSON.parse(JSON.stringify(original)); expected['HeaderManager.headers'][0]['Header.name'] = 'Content-Type'
    expect(wrapper.emitted('update:modelValue')?.[0]?.[0]).toEqual(expected)
    expect(original['HeaderManager.headers'][0]['Header.name']).toBe('Accept')
    wrapper.unmount()
  })

  it('retains header add/delete events and untouched item metadata', async () => {
    const original = find(plan(), 'headers').properties
    const wrapper = mount(HeadersEditor, { props: { modelValue: original },
      global: { plugins: [ElementPlus], stubs: { FunctionHelper: true, Plus: true } } })
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text().includes('editor.headers.add'))!.trigger('click')
    const added = wrapper.emitted('update:modelValue')![0][0] as any
    expect(added['HeaderManager.headers']).toHaveLength(2)
    expect(added['HeaderManager.headers'][0]).toEqual(original['HeaderManager.headers'][0])
    await wrapper.setProps({ modelValue: added })
    await wrapper.findAll('button').filter(button => button.text().includes('editor.common.delete'))[1].trigger('click')
    expect(wrapper.emitted('update:modelValue')![1][0]).toEqual(original)
    wrapper.unmount()
  })
})

function node(id: string, testclass: string, properties: Record<string, any> = {}, children: any[] = []) {
  return { id, testclass, testname: id, enabled: true, properties, children }
}

function plan() {
  const request = node('request', 'HTTPSamplerProxy', {
    'HTTPSampler.method': 'POST', 'HTTPSampler.path': '/before', 'HTTPSampler.domain': '${host}',
    'HTTPSampler.connect_timeout': '${timeout}', 'HTTPSampler.postBodyRaw': false,
    __http_params: [{ _elementType: 'HTTPArgument', _name: 'page', 'Argument.name': 'page', 'Argument.value': '${page}', 'HTTPArgument.always_encode': true, 'HTTPArgument.use_equals': false }],
    'custom.sampler.option': { nested: ['retained'] },
  }, [
    { ...node('headers', 'HeaderManager', { 'HeaderManager.headers': [{ _name: 'Accept', _elementType: 'Header', 'Header.name': 'Accept', 'Header.value': 'application/json', custom: 'keep' }], custom: 'header option' }), enabled: false },
    node('pre', 'JSR223PreProcessor', { script: 'vars.put("x", "1")', scriptLanguage: 'groovy', parameters: 'keep-parameters', cacheKey: 'keep-cache' }),
    { ...node('assert', 'ResponseAssertion', { 'Assertion.test_field': 'Assertion.response_code', 'Assertion.test_type': 4, 'Assertion.test_strings': ['200', '201'], 'Assertion.custom_message': 'keep' }), enabled: false },
    node('json-assert', 'JSONAssertion', { JSON_PATH: '$.ok', EXPECTED_VALUE: 'true', JSONVALIDATION: true }),
    node('extract', 'JSONPostProcessor', { 'JSONPostProcessor.referenceNames': 'token', 'JSONPostProcessor.jsonPathExprs': '$.token', 'JSONPostProcessor.match_numbers': '-1', 'JSONPostProcessor.defaultValues': 'missing' }),
    node('timer', 'ConstantTimer', { 'ConstantTimer.delay': '250' }),
    { ...node('plugin', 'third.party.Plugin', { custom: ['a', 'b'] }), enabled: false },
  ])
  request.enabled = false
  return node('plan', 'TestPlan', { custom: 'plan option' }, [
    node('group1', 'ThreadGroup', { 'ThreadGroup.num_threads': '100', 'ThreadGroup.duration': '300', 'ThreadGroup.ramp_time': '20', 'ThreadGroup.delay': '10', loops: -1, 'ThreadGroup.scheduler': true, 'ThreadGroup.on_sample_error': 'stopthread' }, [
      node('csv', 'CSVDataSet', { filename: 'existing.csv', variableNames: 'page', delimiter: ';', recycle: false, shareMode: 'shareMode.group' }),
      request,
      node('controller', 'LoopController', { loops: 3 }, [node('nested', 'HTTPSamplerProxy', { 'HTTPSampler.path': '/nested' })]),
    ]),
    { ...node('group2', 'ThreadGroup', { 'ThreadGroup.num_threads': 25, 'ThreadGroup.duration': 600 }, [node('other', 'HTTPSamplerProxy', { 'HTTPSampler.path': '/other' })]), enabled: false },
  ])
}

function find(tree: any, id: string): any {
  return tree.id === id ? tree : tree.children?.map((child: any) => find(child, id)).find(Boolean)
}

describe('lossless test case editor', () => {
  it('preserves every original property, disabled flag, child, and group on no-op save', () => {
    const e = editor(); const original = plan(); e.load(original)
    expect(e.buildTreeFromEndpoints()).toEqual(original)
    expect(e.treeData.value).toEqual(plan())
  })

  it('loads original first-group values and exposes nested samplers without flattening', () => {
    const e = editor(); e.load(plan())
    expect(e.threadGroupConfig).toMatchObject({ numThreads: 100, duration: 300, rampTime: 20, delay: 10, loops: -1, scheduler: true, onSampleError: 'stopthread' })
    expect([...e.endpoints.value.keys()]).toEqual(['request', 'nested', 'other'])
    expect(e.csvDatasetConfig).toMatchObject({ filePath: 'existing.csv', variableNames: 'page', delimiter: ';', recycle: false })
  })

  it('changes only the edited sampler scalar and first-group setting', () => {
    const e = editor(); const expected = plan(); e.load(plan())
    e.endpoints.value.get('request').path = '/after'
    e.threadGroupConfig.numThreads = 125
    find(expected, 'request').properties['HTTPSampler.path'] = '/after'
    find(expected, 'group1').properties['ThreadGroup.num_threads'] = 125
    expect(e.buildTreeFromEndpoints()).toEqual(expected)
  })

  it('preserves expression-valued settings without materializing fallback defaults', () => {
    const original = plan(); find(original, 'group1').properties['ThreadGroup.num_threads'] = '${threads}'
    const e = editor(); e.load(original)
    expect(e.buildTreeFromEndpoints()).toEqual(original)
  })

  it('patches one header and script without deleting their unknown settings or siblings', () => {
    const e = editor(); const expected = plan(); e.load(plan())
    e.endpoints.value.get('request').headers[0].value = 'text/plain'
    e.endpoints.value.get('request').preScript.script = 'vars.put("x", "2")'
    find(expected, 'headers').properties['HeaderManager.headers'][0]['Header.value'] = 'text/plain'
    find(expected, 'pre').properties.script = 'vars.put("x", "2")'
    expect(e.buildTreeFromEndpoints()).toEqual(expected)
  })

  it.each(['assertions', 'extractors', 'queryParams', 'cookies', 'auth'])('blocks unsupported %s edits before save and run', async field => {
    const e = editor(); e.load(plan())
    const endpoint = e.endpoints.value.get('request')
    endpoint[field] = field === 'auth' ? { type: 'basic', username: 'u', password: 'p' } : [{ changed: true }]
    await e.handleRun()
    expect(e.api.saveTestPlanTree).not.toHaveBeenCalled()
    expect(e.api.executeTest).not.toHaveBeenCalled()
    expect(e.ElMessage.error).toHaveBeenCalledWith(expect.stringMatching(/JMX/))
    expect(e.treeData.value).toEqual(plan())
    expect(e.running.value).toBe(false)
  })

  it('blocks ambiguous multiple-script edits rather than replacing either script', async () => {
    const e = editor(); const tree = plan()
    find(tree, 'request').children.push(node('pre2', 'JSR223PreProcessor', { script: 'two', scriptLanguage: 'groovy' }))
    e.load(tree); e.endpoints.value.get('request').preScript.script = 'changed'
    expect(await e.handleSave()).toBe(false)
    expect(e.api.saveTestPlanTree).not.toHaveBeenCalled()
  })

  it('keeps nested deletion in its original parent', () => {
    const e = editor(); const expected = plan(); e.load(plan())
    e.deleteEndpoint('nested'); find(expected, 'controller').children = []
    expect(e.buildTreeFromEndpoints()).toEqual(expected)
  })

  it('adds a new endpoint to the selected endpoint parent rather than the first group', () => {
    const e = editor(); e.load(plan()); e.selectEndpoint('other'); e.addEndpoint()
    const id = e.selectedEndpointId.value; const tree = e.buildTreeFromEndpoints()
    expect(find(tree, 'group2').children.map((child: any) => child.id)).toEqual(['other', id])
    expect(find(tree, 'group1')).toEqual(find(plan(), 'group1'))
  })

  it('duplicates an endpoint without stripping its hidden children and properties', () => {
    const e = editor(); e.load(plan()); e.duplicateEndpoint('request')
    const duplicated = find(e.buildTreeFromEndpoints(), e.selectedEndpointId.value)
    const original = find(plan(), 'request')
    expect(duplicated.properties).toEqual(original.properties)
    expect(duplicated.enabled).toBe(false)
    const withoutIds = (value: any): any => Array.isArray(value) ? value.map(withoutIds) : value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).filter(([k]) => k !== 'id').map(([k, v]) => [k, withoutIds(v)])) : value
    expect(withoutIds(duplicated.children)).toEqual(withoutIds(original.children))
    expect(duplicated.children[0].id).not.toBe(original.children[0].id)
  })

  it('preserves a patch across repeated saves', async () => {
    const e = editor(); e.load(plan()); e.endpoints.value.get('other').path = '/changed'
    expect(await e.handleSave()).toBe(true)
    const saved = JSON.parse(JSON.stringify(e.treeData.value))
    expect(await e.handleSave()).toBe(true)
    expect(e.treeData.value).toEqual(saved)
    expect(e.api.saveTestPlanTree).toHaveBeenCalledTimes(2)
  })

  it('reorders sibling samplers without moving their containing groups or controllers', () => {
    const e = editor(); const tree = plan()
    find(tree, 'group2').children.push(node('sibling', 'HTTPSamplerProxy', { 'HTTPSampler.path': '/sibling' }))
    e.load(tree); e.handleReorder(2, 3)
    const built = e.buildTreeFromEndpoints()
    expect(find(built, 'group2').children.map((child: any) => child.id)).toEqual(['sibling', 'other'])
    expect(find(built, 'group1')).toEqual(find(tree, 'group1'))
  })

  it('refuses dragging samplers across different parents with an actionable warning', () => {
    const e = editor(); e.load(plan()); e.handleReorder(0, 2)
    expect([...e.endpoints.value.keys()]).toEqual(['request', 'nested', 'other'])
    expect(e.ElMessage.warning).toHaveBeenCalledWith(expect.stringMatching(/JMX/))
    expect(e.buildTreeFromEndpoints()).toEqual(plan())
  })

  it('sets the script language when adding the first processor', () => {
    const e = editor(); e.load(plan())
    e.endpoints.value.get('other').preScript.script = 'vars.put("x", "1")'
    const pre = find(e.buildTreeFromEndpoints(), 'other').children[0]
    expect(pre.properties).toMatchObject({ scriptLanguage: 'groovy', script: 'vars.put("x", "1")' })
  })

  it('patches CSV values without losing the original class, options, position, or enabled flag', () => {
    const e = editor(); const expected = plan(); e.load(plan()); e.csvDatasetConfig.filePath = 'changed.csv'
    find(expected, 'csv').properties.filename = 'changed.csv'
    expect(e.buildTreeFromEndpoints()).toEqual(expected)
  })

  it('persists a new simple endpoint twice without duplicating it', async () => {
    const e = editor(); e.load(plan()); e.selectEndpoint('other'); e.addEndpoint()
    expect(await e.handleSave()).toBe(true)
    const saved = JSON.parse(JSON.stringify(e.treeData.value))
    expect(await e.handleSave()).toBe(true)
    expect(e.treeData.value).toEqual(saved)
  })

  it('honors sibling reordering of a newly added endpoint on its first save', () => {
    const e = editor(); e.load(plan()); e.selectEndpoint('other'); e.addEndpoint()
    const id = e.selectedEndpointId.value; e.handleReorder(3, 2)
    expect(find(e.buildTreeFromEndpoints(), 'group2').children.map((child: any) => child.id)).toEqual([id, 'other'])
  })

  it('blocks copying a sampler containing nested samplers instead of later deleting their copies', async () => {
    const e = editor(); const tree = plan()
    find(tree, 'request').children.push(node('sub-request', 'HTTPSamplerProxy'))
    e.load(tree); e.duplicateEndpoint('request')
    expect(await e.handleSave()).toBe(false)
    expect(e.api.saveTestPlanTree).not.toHaveBeenCalled()
  })

  it('blocks unsupported new-endpoint form data instead of silently dropping parameters', async () => {
    const e = editor(); e.load(plan()); e.addEndpoint()
    e.endpoints.value.get(e.selectedEndpointId.value).queryParams.push({ name: 'page', value: '1', description: '' })
    expect(await e.handleSave()).toBe(false)
    expect(e.api.saveTestPlanTree).not.toHaveBeenCalled()
  })

  it('patches a raw body without changing unrelated sampler settings or children', () => {
    const tree = plan(); const request = find(tree, 'request')
    delete request.properties.__http_params
    request.properties['HTTPSampler.postBodyRaw'] = true
    request.properties.__body_data = '{"before":true}'
    const e = editor(); e.load(tree); e.endpoints.value.get('request').bodyContent = '{"after":true}'
    const expected = JSON.parse(JSON.stringify(tree)); find(expected, 'request').properties.__body_data = '{"after":true}'
    expect(e.buildTreeFromEndpoints()).toEqual(expected)
  })
})

describe('JMX editor save before run', () => {
  it.each([true, false])('executes only after successful persistence: %s', async saved => {
    const e = editor('src/components/editor/JmxTreeEditor.vue'); e.treeData.value = [plan()]
    if (!saved) e.api.saveTestPlanTree.mockRejectedValue(new Error('save failed'))
    await e.handleRun()
    expect(e.api.executeTest).toHaveBeenCalledTimes(saved ? 1 : 0)
    expect(e.running.value).toBe(false)
  })
})
