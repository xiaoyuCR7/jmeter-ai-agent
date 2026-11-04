// @vitest-environment jsdom
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'

const getExecutionLive = vi.fn()
vi.mock('@/api', () => ({ api: { getExecutionLive: (id: string) => getExecutionLive(id), cancelExecution: vi.fn() } }))
vi.mock('@/composables/useI18n', () => ({ useI18n: () => ({ t: (key: string) => key, L: (zh: string) => zh }) }))
vi.mock('vue-echarts', () => ({ default: { name: 'VChart', props: ['option'], template: '<div class="chart" />' } }))

import LiveRunPanel from '../src/components/LiveRunPanel.vue'
import RunningRuns from '../src/components/RunningRuns.vue'

const point = (t: number, partial = false) => ({ t, requests: 50, throughput: 10, error_rate: 0, avg: 100, p95: 200, threads: 5, partial })
const running = (extra = {}) => ({
  execution_id: 'run-1', status: 'running', progress: 40, plan_name: 'Checkout', project_id: 'p', start_time: null,
  purpose: { kind: 'capacity', step: 1 }, guard: { enabled: true, error_rate: 90 }, available: true,
  elapsed_seconds: 48, planned_seconds: 120, remaining_seconds: 72, planned_threads: 5,
  totals: { requests: 480, errors: 0, error_rate: 0, avg: 100, throughput: 10, threads: 5 },
  window: { seconds: 30, requests: 300, throughput: 10, error_rate: 0, avg: 100, p95: 200 },
  series: [point(0), point(5), point(10, true)], labels: [], ...extra,
})
const mountWith = (component: any, props: any) =>
  mount(component, { props, global: { plugins: [ElementPlus], stubs: { RouterLink: { template: '<a><slot /></a>' } } } })

describe('live run panel', () => {
  beforeEach(() => getExecutionLive.mockReset())

  it('shows the numbers so far and leaves the still-filling point out of the charts', async () => {
    getExecutionLive.mockResolvedValue({ data: running() })
    const wrapper = mountWith(LiveRunPanel, { executionId: 'run-1' })
    await flushPromises()
    expect(wrapper.text()).toContain('Checkout')
    expect(wrapper.text()).toContain('容量探索 · 第 2 级')
    expect(wrapper.text()).toContain('剩余约 1:12')
    const option = wrapper.findAllComponents({ name: 'VChart' })[0].props('option')
    expect(option.xAxis.data).toEqual(['0:00', '0:05'])
    wrapper.unmount()
  })

  it('says it is waiting before the first results, and hands over when the run ends', async () => {
    getExecutionLive.mockResolvedValueOnce({ data: running({ available: false, series: [] }) })
    const wrapper = mountWith(LiveRunPanel, { executionId: 'run-1' })
    await flushPromises()
    expect(wrapper.text()).toContain('等待第一批结果')
    getExecutionLive.mockResolvedValueOnce({ data: running({ status: 'completed', available: false }) })
    await (wrapper.vm as any).$.setupState.poll()
    expect(wrapper.emitted('ended')?.[0]).toEqual(['completed'])
    wrapper.unmount()
  })

  it('gives a card only to the runs still going', async () => {
    getExecutionLive.mockResolvedValue({ data: running() })
    const wrapper = mountWith(RunningRuns, { executions: [
      { id: 'run-1', status: 'running', test_plan_name: 'Checkout' },
      { id: 'run-0', status: 'completed', test_plan_name: 'Old' },
    ] })
    await flushPromises()
    expect(wrapper.findAll('.card')).toHaveLength(1)
    expect(wrapper.text()).toContain('P95 200 ms')
    expect(getExecutionLive).toHaveBeenCalledWith('run-1')
    expect(getExecutionLive).not.toHaveBeenCalledWith('run-0')
    wrapper.unmount()
  })
})
