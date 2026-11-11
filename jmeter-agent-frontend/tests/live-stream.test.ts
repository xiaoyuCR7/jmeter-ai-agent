// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'

const getExecutionLive = vi.fn()
const streamExecutionLive = vi.fn()
const getStorageUsage = vi.fn()
const pruneStorage = vi.fn()
vi.mock('@/api', () => ({ api: {
  getExecutionLive: (id: string) => getExecutionLive(id),
  streamExecutionLive: (id: string, signal?: AbortSignal) => streamExecutionLive(id, signal),
  cancelExecution: vi.fn(),
  getStorageUsage: () => getStorageUsage(),
  pruneStorage: (days: number, include: boolean) => pruneStorage(days, include),
} }))
vi.mock('@/composables/useI18n', () => ({ useI18n: () => ({ t: (key: string) => key, L: (zh: string) => zh }) }))
vi.mock('vue-echarts', () => ({ default: { name: 'VChart', props: ['option'], template: '<div class="chart" />' } }))

import LiveRunPanel from '../src/components/LiveRunPanel.vue'
import StoragePanel from '../src/components/StoragePanel.vue'

const view = (extra = {}) => ({
  execution_id: 'run-1', status: 'running', progress: 40, plan_name: 'Checkout', project_id: 'p', start_time: null,
  purpose: null, guard: { enabled: true, error_rate: 90 }, available: true,
  elapsed_seconds: 48, planned_seconds: 120, remaining_seconds: 72, planned_threads: 5,
  totals: { requests: 480, errors: 0, error_rate: 0, avg: 100, throughput: 10, threads: 5 },
  window: { seconds: 30, requests: 300, throughput: 10, error_rate: 0, avg: 100, p95: 200 },
  series: [], labels: [], ...extra,
})

// A response whose body delivers the given chunks, then ends or fails.
function streamOf(chunks: string[], fail = false) {
  const encoder = new TextEncoder()
  let index = 0
  return {
    ok: true,
    status: 200,
    body: { getReader: () => ({ read: async () => {
      if (index < chunks.length) return { done: false, value: encoder.encode(chunks[index++]) }
      if (fail) throw new Error('connection lost')
      return { done: true, value: undefined }
    } }) },
  }
}
const event = (data: unknown) => `data: ${JSON.stringify(data)}\n\n`
const mountWith = (component: any, props: any = {}) =>
  mount(component, { props, global: { plugins: [ElementPlus], stubs: { RouterLink: { template: '<a><slot /></a>' } } } })

describe('live run panel over the event stream', () => {
  beforeEach(() => { getExecutionLive.mockReset(); streamExecutionLive.mockReset() })

  it('shows what the server pushes and never polls while the stream works', async () => {
    streamExecutionLive.mockResolvedValue(streamOf([
      event(view()), ': keep-alive\n\n',
      event(view({ totals: { requests: 960, errors: 0, error_rate: 0, avg: 100, throughput: 20, threads: 5 } })),
      event(view({ status: 'completed', available: false })),
    ]))
    const wrapper = mountWith(LiveRunPanel, { executionId: 'run-1' })
    await flushPromises()
    expect(streamExecutionLive).toHaveBeenCalledTimes(1)
    expect(getExecutionLive).not.toHaveBeenCalled()
    expect(wrapper.emitted('ended')).toEqual([['completed']])
    wrapper.unmount()
  })

  it('falls back to polling when the stream breaks before the run ends', async () => {
    streamExecutionLive.mockResolvedValue(streamOf([event(view())], true))
    getExecutionLive.mockResolvedValue({ data: view({ plan_name: 'From a poll' }) })
    const wrapper = mountWith(LiveRunPanel, { executionId: 'run-1' })
    await flushPromises()
    expect(getExecutionLive).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('From a poll')
    expect(wrapper.emitted('ended')).toBeUndefined()
    wrapper.unmount()
  })

  it('falls back to polling when the backend has no stream', async () => {
    streamExecutionLive.mockResolvedValue({ ok: false, status: 404, body: null })
    getExecutionLive.mockResolvedValue({ data: view() })
    const wrapper = mountWith(LiveRunPanel, { executionId: 'run-1' })
    await flushPromises()
    expect(getExecutionLive).toHaveBeenCalledTimes(1)
    expect(wrapper.text()).toContain('Checkout')
    wrapper.unmount()
  })
})

describe('report storage panel', () => {
  beforeEach(() => { getStorageUsage.mockReset(); pruneStorage.mockReset() })

  it('shows how much the reports hold and whether pruning is automatic', async () => {
    getStorageUsage.mockResolvedValue({ data: {
      reports_dir: 'D:/reports', runs: 37, total_bytes: 115 * 1024 ** 2, dashboard_bytes: 110 * 1024 ** 2,
      result_bytes: 2 * 1024 ** 2, retention_days: 0, retention_includes_results: false } })
    const wrapper = mountWith(StoragePanel)
    await flushPromises()
    expect(wrapper.text()).toContain('37')
    expect(wrapper.text()).toContain('115.0 MB')
    expect(wrapper.text()).toContain('110.0 MB')
    expect(wrapper.text()).toContain('自动清理未开启')
    wrapper.unmount()
  })

  it('says so when automatic pruning is on', async () => {
    getStorageUsage.mockResolvedValue({ data: {
      reports_dir: 'D:/reports', runs: 1, total_bytes: 2 * 1024 ** 3, dashboard_bytes: 0, result_bytes: 0,
      retention_days: 30, retention_includes_results: true } })
    const wrapper = mountWith(StoragePanel)
    await flushPromises()
    expect(wrapper.text()).toContain('2.00 GB')
    expect(wrapper.text()).toContain('每天清理 30 天前的运行')
    wrapper.unmount()
  })
})
