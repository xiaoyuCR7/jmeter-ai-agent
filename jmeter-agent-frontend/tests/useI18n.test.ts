import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useI18n } from '@/composables/useI18n'

const mockAppStore = {
  currentLang: 'zh-CN',
  setLang: vi.fn()
}

vi.mock('@/store', () => ({
  useAppStore: vi.fn(() => mockAppStore)
}))

describe('useI18n', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockAppStore.currentLang = 'zh-CN'
  })

  afterEach(() => {
    mockAppStore.currentLang = 'zh-CN'
  })

  it('should return Chinese translation by default', () => {
    const { t } = useI18n()
    expect(t('layout.nav.projects')).toBe('项目')
    expect(t('editor.view.form')).toBe('表单视图')
  })

  it('should return English translation when language is English', () => {
    mockAppStore.currentLang = 'en'
    const { t } = useI18n()
    expect(t('layout.nav.projects')).toBe('Projects')
    expect(t('editor.view.tree')).toBe('JMX tree')
  })

  it('should support interpolation', () => {
    const { t } = useI18n()
    expect(t('createPlan.apiCount', { count: 10 })).toBe('接口数量: 10')
    expect(t('testPlans.confirmDeleteProject', { name: 'demo' })).toContain('"demo"')
  })

  it('picks the text for the current language with L, and follows a language switch', () => {
    const { L } = useI18n()
    expect(L('取消', 'Cancel')).toBe('取消')
    mockAppStore.currentLang = 'en'
    expect(L('取消', 'Cancel')).toBe('Cancel')
  })

  it('leaves no component defining its own copy of L', () => {
    const sources = import.meta.glob('../src/**/*.vue', { query: '?raw', import: 'default', eager: true }) as Record<string, string>
    const copies = Object.entries(sources).filter(([, source]) => /const L = \(/.test(source)).map(([file]) => file)
    expect(copies).toEqual([])
  })

  it('should return fallback for missing keys', () => {
    const { t } = useI18n()
    expect(t('non.existent.key')).toBe('non.existent.key')
    expect(t('non.existent.key', null, 'fallback')).toBe('fallback')
  })

  it('has a title for every route', () => {
    const { t } = useI18n()
    const router = readFileSync(resolve('src/router/index.ts'), 'utf8')
    const keys = [...router.matchAll(/titleKey: '([\w.]+)'/g)].map(m => m[1])
    expect(keys.length).toBeGreaterThan(0)
    for (const key of keys) expect(t(key)).not.toBe(key)
  })

  it('should set language correctly', () => {
    const { setLang } = useI18n()
    setLang('en')
    expect(mockAppStore.setLang).toHaveBeenCalledWith('en')
  })
})
