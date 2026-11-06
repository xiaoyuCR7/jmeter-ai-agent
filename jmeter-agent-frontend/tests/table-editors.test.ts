// @vitest-environment jsdom
// Each table editor of the plan tree has to hand a typed edit back to the plan, under JMeter's own names.
import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import CookiesEditor from '../src/components/editor/CookiesEditor.vue'
import AuthEditor from '../src/components/editor/AuthEditor.vue'
import DnsHostsEditor from '../src/components/editor/DnsHostsEditor.vue'
import VariablesEditor from '../src/components/editor/VariablesEditor.vue'
import AssertionPatternsEditor from '../src/components/editor/AssertionPatternsEditor.vue'
import UserParametersEditor from '../src/components/editor/UserParametersEditor.vue'
import HttpParamsEditor from '../src/components/editor/HttpParamsEditor.vue'

vi.mock('@/composables/useI18n', () => ({ useI18n: () => ({ t: (key: string) => key, L: (zh: string) => zh }) }))

async function mountEditor(component: any, modelValue: Record<string, any>) {
  const wrapper = mount(component, { props: { modelValue }, global: { plugins: [ElementPlus], stubs: { FunctionHelper: true, Plus: true } } })
  await flushPromises()
  return wrapper
}

function last(wrapper: any) {
  const events = wrapper.emitted('update:modelValue')
  return events?.[events.length - 1]?.[0]
}

describe('table editors write what was typed', () => {
  it('a cookie is named by its element name, as JMeter reads it', async () => {
    const wrapper = await mountEditor(CookiesEditor, { 'CookieManager.cookies': [
      { _elementType: 'Cookie', _name: 'session', 'Cookie.value': 'a', 'Cookie.domain': 'shop', 'Cookie.path': '/' }] })
    const inputs = wrapper.findAll('input')
    expect((inputs[0].element as HTMLInputElement).value).toBe('session')  // A loaded cookie shows its name.
    await inputs[0].setValue('sid')
    const cookie = last(wrapper)['CookieManager.cookies'][0]
    expect(cookie._name).toBe('sid')
    expect(cookie).not.toHaveProperty('Cookie.name')
    expect(cookie['Cookie.value']).toBe('a')
  })

  it('an auth entry, a DNS host, a variable, a pattern and a user parameter keep the typed value', async () => {
    const cases: [any, Record<string, any>, (value: any) => any][] = [
      [AuthEditor, { 'AuthManager.auth_list': [{ _elementType: 'Authorization', 'Authorization.url': '' }] },
        value => value['AuthManager.auth_list'][0]['Authorization.url']],
      [DnsHostsEditor, { 'DNSCacheManager.hosts': [{ _elementType: 'StaticHost', 'StaticHost.Name': '' }] },
        value => value['DNSCacheManager.hosts'][0]['StaticHost.Name']],
      [VariablesEditor, { 'Arguments.arguments': [{ _elementType: 'Argument', 'Argument.name': '' }] },
        value => value['Arguments.arguments'][0]['Argument.name']],
      [HttpParamsEditor, { __http_params: [{ 'Argument.name': '', 'Argument.value': '' }] },
        value => value.__http_params[0]['Argument.name']],
      [AssertionPatternsEditor, { 'Assertion.test_strings': [''] }, value => value['Assertion.test_strings'][0]],
    ]
    const params = await mountEditor(UserParametersEditor, { 'UserParameters.names': ['user'], 'UserParameters.thread_values': [['']] })
    const [name, value] = params.findAll('input')
    await name.setValue('account')
    expect(last(params)['UserParameters.names']).toEqual(['account'])
    await value.setValue('alice')
    expect(last(params)['UserParameters.thread_values']).toEqual([['alice']])
    for (const [component, model, read] of cases) {
      const wrapper = await mountEditor(component, model)
      await wrapper.find('input:not([type=file])').setValue('typed')
      expect(read(last(wrapper)), component.__name).toBe('typed')
    }
  })
})
