import { describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { useEndpointList } from '../src/composables/useEndpointList'

function setup(entries: Array<[string, any]> = []) {
  const endpoints = ref(new Map<string, any>(entries))
  const selectedId = ref<string | null>(entries[0]?.[0] ?? null)
  const notify = { success: vi.fn(), warning: vi.fn() }
  const onSelect = vi.fn()
  const list = useEndpointList({
    endpoints, selectedId, notify, onSelect,
    t: (key: string) => ({ 'common.unnamed': 'Unnamed', 'editor.newEndpoint': 'New request', 'editor.copySuffix': ' (copy)' } as Record<string, string>)[key] ?? key,
    L: (_zh: string, en: string) => en,
  })
  return { endpoints, selectedId, notify, onSelect, ...list }
}
const request = (id: string, parent: string, extra = {}) =>
  [id, { id, _parentId: parent, name: id.toUpperCase(), method: 'POST', path: `/${id}`, ...extra }] as [string, any]

describe('the request list of the plan editor', () => {
  it('lists requests in run order, with defaults for what a request leaves empty', () => {
    const { endpointList } = setup([request('a', 'group'), ['b', { id: 'b', _parentId: 'group', name: '', method: '', path: '' }]])
    expect(endpointList.value).toEqual([
      { id: 'a', name: 'A', method: 'POST', path: '/a' },
      { id: 'b', name: 'Unnamed', method: 'GET', path: '/' },
    ])
  })

  it('adds a request beside the selected one and selects it', () => {
    const { endpoints, selectedId, addEndpoint, onSelect } = setup([request('a', 'controller-7')])
    addEndpoint()
    const added = endpoints.value.get(selectedId.value!)
    expect(endpoints.value.size).toBe(2)
    expect(added).toMatchObject({ name: 'New request', method: 'GET', path: '/', _parentId: 'controller-7', bodyType: 'none' })
    expect(onSelect).toHaveBeenCalledWith(added.id)
  })

  it('copies a request without sharing its data', () => {
    const { endpoints, selectedId, duplicateEndpoint, notify } = setup([request('a', 'group', { headers: [{ name: 'X', value: '1' }] })])
    duplicateEndpoint('a')
    const copy = endpoints.value.get(selectedId.value!)
    expect(copy.name).toBe('A (copy)')
    copy.headers[0].value = 'changed'
    expect(endpoints.value.get('a').headers[0].value).toBe('1')
    expect(notify.success).toHaveBeenCalled()
    duplicateEndpoint('missing')
    expect(endpoints.value.size).toBe(2)
  })

  it('selects another request when the selected one is removed', () => {
    const { endpoints, selectedId, deleteEndpoint } = setup([request('a', 'group'), request('b', 'group')])
    deleteEndpoint('a')
    expect(selectedId.value).toBe('b')
    deleteEndpoint('b')
    expect(selectedId.value).toBeNull()
    expect(endpoints.value.size).toBe(0)
  })

  it('reorders within one parent and refuses to cross into another', () => {
    const { endpoints, reorderEndpoint, notify } = setup([request('a', 'g1'), request('b', 'g1'), request('c', 'g2')])
    reorderEndpoint(0, 1)
    expect(Array.from(endpoints.value.keys())).toEqual(['b', 'a', 'c'])
    reorderEndpoint(0, 2)
    expect(Array.from(endpoints.value.keys())).toEqual(['b', 'a', 'c'])
    expect(notify.warning).toHaveBeenCalledWith(expect.stringContaining('cannot be moved across'))
  })
})
