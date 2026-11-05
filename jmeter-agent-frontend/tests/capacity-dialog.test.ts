// @vitest-environment jsdom
import { describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'

// A search as the backend returns it after stopping at its first load: the higher loads never ran.
const stoppedEarly = {
  id: 's1', plan_id: 'plan-1', project_id: 'p', status: 'finished', max_error_rate: 10, created_at: '2026-11-05T07:00:00',
  stop_reason: '10 线程：性能需求不达标', capacity_threads: null, capacity_throughput: null,
  steps: [
    { threads: 10, execution_id: 'run-1', status: 'stopped', throughput: 56.8, p95: 231, error_rate: 1.79, requirements: 'fail', note: '性能需求不达标' },
    { threads: 20, execution_id: null, status: 'skipped', throughput: null, p95: null, error_rate: null, requirements: null, note: '' },
    { threads: 40, execution_id: null, status: 'skipped', throughput: null, p95: null, error_rate: null, requirements: null, note: '' },
  ],
}
vi.mock('@/api', () => ({ api: {
  listCapacitySearches: vi.fn(async () => ({ data: [stoppedEarly] })),
  getCapacitySearch: vi.fn(async () => ({ data: stoppedEarly })),
} }))
vi.mock('@/composables/useI18n', () => ({ useI18n: () => ({ t: (key: string) => key, L: (zh: string) => zh }) }))

import CapacitySearchDialog from '../src/components/CapacitySearchDialog.vue'

describe('capacity search dialog', () => {
  it('shows loads a search never reached as not run, not as waiting', async () => {
    const wrapper = mount(CapacitySearchDialog, {
      props: { modelValue: false, plan: { id: 'plan-1', test_config: { concurrent_users: 30 } } },
      global: { plugins: [ElementPlus], stubs: { RouterLink: { template: '<a><slot /></a>' } } },
      attachTo: document.body,
    })
    await wrapper.setProps({ modelValue: true })
    await flushPromises()
    const tags = [...document.body.querySelectorAll('.el-table .el-tag')].map(tag => tag.textContent?.trim())
    expect(tags).toEqual(['停止点', '未运行', '未运行'])
    expect(document.body.textContent).toContain('10 线程：性能需求不达标')
    expect(document.body.textContent).not.toContain('等待')
    wrapper.unmount()
  })
})
