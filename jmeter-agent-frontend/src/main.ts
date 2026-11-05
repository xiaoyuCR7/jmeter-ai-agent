import { createApp } from 'vue'
import { createPinia } from 'pinia'
import 'element-plus/dist/index.css'
import '@/styles/tokens.css'
import '@/styles/theme.css'
import '@/styles/base.css'

import App from './App.vue'
import router from './router'
import { useAppStore } from '@/store'
import { bootstrapSession } from '@/api/session'

// Tells the server's stand-in for a missing script that an app is running here (see UIAssets).
;(window as unknown as { __jmeterAgentBooted?: boolean }).__jmeterAgentBooted = true

// A tab opened before an upgrade asks for page chunks the new build replaced and stayed blank:
// open the page it was going to in the new build instead, once. The marker stops a loop when a chunk
// is missing for good, and the error then shows as before. (The server answers an old page's missing
// entry script the same way: see UIAssets in app/main.py.)
router.onError((error, to) => {
  if (!/dynamically imported module|Importing a module script failed|Failed to fetch|preload/i.test(String(error?.message))) return
  try {
    if (sessionStorage.getItem('jmeter-agent-reloaded') === to.fullPath) return
    sessionStorage.setItem('jmeter-agent-reloaded', to.fullPath)
  } catch { /* No storage: go anyway. */ }
  window.location.assign(to.fullPath)
})
// A page that opened proves its chunks loaded; only then may a later missing chunk reload again.
router.afterEach((_to, _from, failure) => {
  if (failure) return
  try {
    sessionStorage.removeItem('jmeter-agent-reloaded')
    sessionStorage.removeItem('jmeter-agent-stale')  // Set by the server's stand-in for a script an old page asked for.
  } catch { /* nothing to clear */ }
})

const app = createApp(App)

const pinia = createPinia()
app.use(pinia)

// Initialize theme and language before mounting
const appStore = useAppStore()
appStore.initTheme()
appStore.initLang()

app.use(router)

// Sync Element Plus locale with the saved language
appStore.setLang(appStore.currentLang)

bootstrapSession().finally(() => app.mount('#app'))
