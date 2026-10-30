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
