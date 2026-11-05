// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'

// A trial as the backend returns it when the login's token path is wrong.
const trial = {
  status: 'failed', error: null, duration_seconds: 9.2, environment: null, passed: 0, failed: 1,
  samples: [{
    label: 'POST /api/orders', method: 'POST', url: 'http://127.0.0.1:18080/api/orders', code: '401', message: 'Unauthorized',
    success: false, elapsed_ms: 1, failures: ['Expected status 201'], content_type: 'application/json',
    request_headers: 'Authorization: Bearer NOT_FOUND\nContent-Type: application/json', request: '{"product_id": 1}',
    response: '{"error": "not logged in"}', response_truncated: false,
  }],
}
vi.mock('@/api', () => ({ api: { trialRun: vi.fn(async () => ({ data: trial })) } }))
vi.mock('@/composables/useI18n', () => ({ useI18n: () => ({ t: (key: string) => key, L: (zh: string) => zh, currentLang: { value: 'zh-CN' } }) }))

import TrialRunDialog from '../src/components/TrialRunDialog.vue'

describe('trial dialog', () => {
  it('shows what each request sent next to what came back', async () => {
    const wrapper = mount(TrialRunDialog, {
      props: { modelValue: false, plans: [{ id: 'plan-1', name: 'Orders' }], initialPlanId: 'plan-1' },
      global: { plugins: [ElementPlus] }, attachTo: document.body,
    })
    await wrapper.setProps({ modelValue: true })
    const start = [...document.body.querySelectorAll('.el-dialog button')].find(b => b.textContent?.includes('开始试跑'))
    ;(start as HTMLElement).click()
    await flushPromises()
    ;(document.body.querySelector('.el-dialog .el-table__expand-icon') as HTMLElement).click()
    await flushPromises()
    const detail = document.body.querySelector('.el-dialog .el-table__expanded-cell')?.textContent ?? ''
    expect(detail).toContain('发送的请求')
    expect(detail).toContain('Authorization: Bearer NOT_FOUND')  // The clue to the 401, left unmasked.
    expect(detail).toContain('{"product_id": 1}')
    expect(detail).toContain('响应 · application/json')
    wrapper.unmount()
  })
})
