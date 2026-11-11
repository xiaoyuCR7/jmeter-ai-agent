/**
 * The requests of the plan being edited, as the list on the left of the editor works with them:
 * which one is selected, and adding, copying, removing and reordering.
 *
 * The map is keyed by request ID and its order is the order requests run in. Each request remembers
 * the tree node it sits under (_parentId), which a reorder must not change: moving a request across
 * thread groups or controllers is a structural edit for the JMX tree editor.
 */
import { computed, type Ref } from 'vue'
import type { EndpointItem } from '@/components/editor/EndpointList.vue'

type Translate = (key: string) => string
type Notify = { success: (message: string) => void; warning: (message: string) => void }

export function newEndpoint(id: string, name: string, parentId?: string) {
  return {
    id,
    _parentId: parentId,
    name,
    description: '',
    method: 'GET',
    path: '/',
    domain: '',
    port: '',
    protocol: '',
    headers: [],
    cookies: [],
    queryParams: [],
    bodyType: 'none',
    bodyContent: '',
    auth: { type: 'none', username: '', password: '' },
    preScript: { language: 'groovy', script: '' },
    postScript: { language: 'groovy', script: '' },
    assertions: [],
    extractors: [],
    settings: { timeout: 0, followRedirects: true, autoRedirects: false, keepAlive: true, encoding: '' },
  }
}

const randomId = () => Math.random().toString(36).substring(2, 10)

export function useEndpointList(options: {
  endpoints: Ref<Map<string, any>>
  selectedId: Ref<string | null>
  t: Translate
  L: (zh: string, en: string) => string
  notify: Notify
  /** Called after a request becomes the selected one, so the editor can show it. */
  onSelect?: (id: string) => void
}) {
  const { endpoints, selectedId, t, L, notify } = options

  const endpointList = computed<EndpointItem[]>(() => {
    const result: EndpointItem[] = []
    // The map holds parsed requests, not tree nodes: reading testname/properties listed every one as "unnamed GET /".
    endpoints.value.forEach((endpoint, id) => {
      result.push({ id, name: endpoint.name || t('common.unnamed'), method: endpoint.method || 'GET', path: endpoint.path || '/' })
    })
    return result
  })

  function selectEndpoint(id: string) {
    selectedId.value = id
    options.onSelect?.(id)
  }

  function addEndpoint() {
    const id = randomId()
    // Next to the selected request, under the same thread group or controller.
    endpoints.value.set(id, newEndpoint(id, t('editor.newEndpoint'), endpoints.value.get(selectedId.value || '')?._parentId))
    selectEndpoint(id)
  }

  function deleteEndpoint(id: string) {
    endpoints.value.delete(id)
    if (selectedId.value === id) {
      selectedId.value = endpoints.value.keys().next().value || null
    }
  }

  function duplicateEndpoint(id: string) {
    const original = endpoints.value.get(id)
    if (!original) return
    const copy = JSON.parse(JSON.stringify(original))
    copy.id = randomId()
    copy.name = original.name + t('editor.copySuffix')
    endpoints.value.set(copy.id, copy)
    selectEndpoint(copy.id)
    notify.success(t('editor.message.endpointCopied'))
  }

  function reorderEndpoint(fromIndex: number, toIndex: number) {
    const entries = Array.from(endpoints.value.entries())
    const movedParent = entries[fromIndex]?.[1]._parentId
    const crossed = entries.slice(Math.min(fromIndex, toIndex), Math.max(fromIndex, toIndex) + 1)
    if (crossed.some(([, endpoint]) => endpoint._parentId !== movedParent)) {
      notify.warning(L('无法跨线程组或控制器移动接口；请使用右上角 JMX 编辑器调整结构。',
        'A request cannot be moved across thread groups or controllers; use the JMX editor at the top right to change the structure.'))
      return
    }
    const [removed] = entries.splice(fromIndex, 1)
    entries.splice(toIndex, 0, removed)
    endpoints.value = new Map(entries)
    if (selectedId.value && endpoints.value.has(selectedId.value)) selectEndpoint(selectedId.value)
  }

  return { endpointList, selectEndpoint, addEndpoint, deleteEndpoint, duplicateEndpoint, reorderEndpoint }
}
