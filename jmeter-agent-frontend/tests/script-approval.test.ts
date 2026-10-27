import { beforeEach, expect, it, vi } from 'vitest'

const confirm = vi.fn()
vi.mock('element-plus', () => ({ ElMessageBox: { confirm } }))

const { withScriptApproval } = await import('../src/api/scriptApproval')

const needsApproval = {
  response: { status: 409, data: { detail: { code: 'scripts_need_approval', findings: [{ name: 'Run shell', kind: 'SystemSampler' }] } } },
}

beforeEach(() => { confirm.mockReset() })

it('retries with allow_scripts only after the user approves the listed scripts', async () => {
  confirm.mockResolvedValue('confirm')
  const send = vi.fn().mockRejectedValueOnce(needsApproval).mockResolvedValueOnce('started')
  await expect(withScriptApproval(send)).resolves.toBe('started')
  expect(send.mock.calls).toEqual([[false], [true]])
  expect(confirm).toHaveBeenCalledOnce()
})

it('a declined approval never sends allow_scripts and reports a readable reason', async () => {
  confirm.mockImplementation(() => Promise.reject(new Error('cancel')))  // what ElMessageBox does on Cancel
  const send = vi.fn(() => Promise.reject(needsApproval))
  const error: any = await withScriptApproval(send).catch(e => e)
  expect(send.mock.calls).toEqual([[false]])
  expect(typeof error.response.data.detail).toBe('string')
})

it('other failures pass through without asking', async () => {
  const conflict = { response: { status: 409, data: { detail: 'Execution ID already exists' } } }
  const send = vi.fn().mockRejectedValue(conflict)
  await expect(withScriptApproval(send)).rejects.toBe(conflict)
  expect(confirm).not.toHaveBeenCalled()
})
