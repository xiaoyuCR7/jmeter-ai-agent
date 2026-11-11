/**
 * useI18n — lightweight i18n composable for Vue 3 components.
 *
 * Uses the Pinia app store for language state. The shared texts are in
 * src/locales/messages.ts, bundled with the app so there is nothing to load.
 *
 * Usage:
 *   const { t, currentLang, setLang } = useI18n()
 *   t('settings.language')                         // '界面语言' / 'Language'
 *   t('env.variableCount', { count: varCount })    // '{{ count }} 个变量'
 *   L('取消', 'Cancel')                             // text one component uses
 *
 * Use t() with a key for text several components share (navigation, routes, settings),
 * and L() for text that belongs to one component.
 */

import { computed } from 'vue'
import { useAppStore, type SupportedLang } from '@/store'

import { messages } from '@/locales/messages'

export function useI18n() {
  const appStore = useAppStore()

  const currentLang = computed(() => appStore.currentLang)

  function t(
    key: string,
    params?: Record<string, string | number> | string,
    fallback?: string
  ): string {
    let values: Record<string, string | number> | undefined
    let fb: string | undefined

    if (typeof params === 'string') {
      fb = params
    } else {
      values = params
      fb = fallback
    }

    const entry = messages[key]
    let text = entry?.[appStore.currentLang as SupportedLang] ?? entry?.['zh-CN'] ?? fb ?? key

    if (values) {
      text = text.replace(/\{\{\s*(\w+)\s*\}\}/g, (match: string, name: string) =>
        String(values![name] ?? match)
      )
    }

    return text
  }

  function setLang(lang: SupportedLang) {
    appStore.setLang(lang)
  }

  /** Text used in one component only: both languages side by side, so neither can be forgotten. */
  function L(zh: string, en: string): string {
    return appStore.currentLang === 'en' ? en : zh
  }

  return { t, L, currentLang, setLang }
}
