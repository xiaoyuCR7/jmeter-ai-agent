<template>
  <div class="app-shell" :class="{ collapsed: sidebarCollapsed }">
    <ToolConfirmations />
    <div v-if="mobileSidebarVisible" class="mobile-overlay" @click="mobileSidebarVisible = false"></div>

    <aside class="sidebar" :class="{ 'mobile-open': mobileSidebarVisible }">
      <div class="brand">
        <img class="brand-mark" src="/logo.svg" alt="" />
        <span class="brand-name">JMeter Agent</span>
      </div>

      <router-link to="/start" class="start-btn" :title="t('layout.start')" @click="mobileSidebarVisible = false">
        <el-icon><Plus /></el-icon>
        <span>{{ t('layout.start') }}</span>
      </router-link>

      <nav class="nav">
        <router-link v-for="item in navItems" :key="item.path" :to="item.path"
          :class="['nav-item', { active: activeMenu === item.path }]" :title="t(item.label)"
          @click="mobileSidebarVisible = false">
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ t(item.label) }}</span>
        </router-link>
      </nav>

      <div class="sidebar-footer">
        <router-link to="/settings" class="ai-status" :class="{ connected: aiConnected }"
          :title="aiConnected ? t('layout.ai.connected') : t('layout.ai.disconnected')">
          <span class="status-dot"></span>
          <span>{{ aiConnected ? t('layout.ai.connected') : t('layout.ai.disconnected') }}</span>
        </router-link>
        <button class="collapse-btn" :title="sidebarCollapsed ? t('layout.expand') : t('layout.collapse')"
          @click="sidebarCollapsed = !sidebarCollapsed">
          <el-icon :class="{ rotated: sidebarCollapsed }"><ArrowLeft /></el-icon>
        </button>
      </div>
    </aside>

    <div class="main">
      <header class="topbar">
        <button class="hamburger-btn" @click="mobileSidebarVisible = !mobileSidebarVisible">
          <el-icon><Menu /></el-icon>
        </button>
        <h1 class="page-title">{{ pageTitle }}</h1>
        <div v-if="resources" class="resource-bar" :title="t('layout.resources')">
          <span class="res-item" :class="level(resources.cpu_percent)">CPU {{ resources.cpu_percent }}%</span>
          <span class="res-item" :class="level(resources.memory_percent)">MEM {{ resources.memory_percent }}%</span>
        </div>
      </header>

      <main class="content">
        <router-view />
      </main>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '@/api'
import ToolConfirmations from '@/components/ToolConfirmations.vue'
import { ArrowLeft, Menu, Folder, VideoPlay, Setting, Plus } from '@element-plus/icons-vue'
import { useI18n } from '@/composables/useI18n'

const route = useRoute()
const { t } = useI18n()
const aiConnected = ref(false)
const sidebarCollapsed = ref(false)
const mobileSidebarVisible = ref(false)
const resources = ref<any>(null)
let resourceTimer: ReturnType<typeof setInterval> | undefined
let disposed = false
let resourceRequestPending = false

const navItems = [
  { path: '/projects', label: 'layout.nav.projects', icon: Folder },
  { path: '/executions', label: 'layout.nav.executions', icon: VideoPlay },
  { path: '/settings', label: 'layout.nav.settings', icon: Setting },
]
const activeMenu = computed(() => (route.meta?.nav as string) || route.path)
const pageTitle = computed(() => (route.meta?.titleKey ? t(route.meta.titleKey as string) : 'JMeter Agent'))
const level = (percent: number) => (percent > 85 ? 'danger' : percent > 70 ? 'warn' : '')

const fetchResources = async () => {
  if (disposed || resourceRequestPending) return
  resourceRequestPending = true
  try {
    const res = await api.checkResources()
    if (!disposed) resources.value = res.data
  } catch { /* ignore */ }
  finally { resourceRequestPending = false }
}

onMounted(async () => {
  try {
    await api.getModels()
    aiConnected.value = true
  } catch {
    aiConnected.value = false
  }
  if (disposed) return
  fetchResources()
  resourceTimer = setInterval(fetchResources, 15000)
})
onUnmounted(() => {
  disposed = true
  if (resourceTimer !== undefined) clearInterval(resourceTimer)
})
</script>

<style scoped lang="scss">
.app-shell {
  --sidebar-width: 224px;
  display: flex;
  min-height: 100vh;
  background: var(--app-bg);

  &.collapsed { --sidebar-width: 60px; }
}

.mobile-overlay {
  display: none;
  @media (max-width: 768px) {
    display: block;
    position: fixed;
    inset: 0;
    background: rgba(14, 20, 29, 0.5);
    z-index: 999;
  }
}

.sidebar {
  width: var(--sidebar-width);
  flex-shrink: 0;
  position: sticky;
  top: 0;
  height: 100vh;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px 10px;
  box-sizing: border-box;
  background: var(--app-sidebar);
  border-right: 1px solid var(--app-sidebar-border);
  transition: width 0.18s ease;

  @media (max-width: 768px) {
    position: fixed;
    left: 0;
    z-index: 1000;
    width: 224px;
    transform: translateX(-100%);
    transition: transform 0.2s ease;
    &.mobile-open { transform: none; box-shadow: var(--app-shadow-lg); }
  }
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 36px;
  padding: 0 6px;
  margin-bottom: 8px;

  .brand-mark {
    width: 28px;
    height: 28px;
    flex-shrink: 0;
    display: block;
  }
  .brand-name { font-weight: 600; font-size: 15px; color: var(--app-sidebar-text-strong); white-space: nowrap; }
}

.start-btn, .nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 36px;
  padding: 0 10px;
  border-radius: var(--app-radius);
  font-size: 14px;
  text-decoration: none;
  white-space: nowrap;
  overflow: hidden;
  .el-icon { font-size: 16px; flex-shrink: 0; }
}

.start-btn {
  margin-bottom: 8px;
  background: var(--app-primary);
  color: var(--app-on-primary);
  font-weight: 500;
  transition: background-color 0.12s;
  &:hover { background: var(--app-primary-hover); }
}

.nav { display: flex; flex-direction: column; gap: 2px; flex: 1; }

.nav-item {
  color: var(--app-sidebar-text);
  transition: background-color 0.12s, color 0.12s;
  &:hover { background: var(--app-sidebar-hover); color: var(--app-sidebar-text-strong); }
  &.active { background: var(--app-sidebar-active); color: var(--app-sidebar-active-text); font-weight: 500; }
}

.sidebar-footer {
  display: flex;
  align-items: center;
  gap: 6px;
  border-top: 1px solid var(--app-sidebar-border);
  padding-top: 10px;

  .ai-status {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 0 6px;
    font-size: 12px;
    color: var(--app-sidebar-text);
    text-decoration: none;
    white-space: nowrap;
    overflow: hidden;

    .status-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; background: var(--app-danger); }
    &.connected .status-dot { background: var(--app-success); }
  }

  .collapse-btn {
    width: 28px;
    height: 28px;
    flex-shrink: 0;
    border: none;
    border-radius: var(--app-radius);
    background: transparent;
    color: var(--app-sidebar-text);
    cursor: pointer;
    display: grid;
    place-items: center;
    &:hover { background: var(--app-sidebar-hover); color: var(--app-sidebar-text-strong); }
    .rotated { transform: rotate(180deg); }
    @media (max-width: 768px) { display: none; }
  }
}

.collapsed .sidebar {
  .brand-name, .start-btn span, .nav-item span, .ai-status span:not(.status-dot) { display: none; }
  .start-btn, .nav-item { justify-content: center; padding: 0; }
  .sidebar-footer { flex-direction: column; }
  .ai-status { justify-content: center; padding: 0; }
}

.main { flex: 1; min-width: 0; display: flex; flex-direction: column; }

.topbar {
  position: sticky;
  top: 0;
  z-index: 10;
  height: 52px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 24px;
  background: var(--app-surface);
  border-bottom: 1px solid var(--app-border);

  @media (max-width: 768px) { padding: 0 16px; }

  .page-title {
    flex: 1;
    min-width: 0;
    margin: 0;
    font-size: 16px;
    font-weight: 600;
    color: var(--app-text);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .hamburger-btn {
    display: none;
    width: 32px;
    height: 32px;
    border: 1px solid var(--app-border);
    border-radius: var(--app-radius);
    background: var(--app-surface);
    color: var(--app-text-secondary);
    cursor: pointer;
    @media (max-width: 768px) { display: grid; place-items: center; }
  }

  .resource-bar {
    display: flex;
    gap: 6px;
    font-size: 12px;
    font-variant-numeric: tabular-nums;
    @media (max-width: 768px) { display: none; }

    .res-item {
      padding: 2px 8px;
      border-radius: var(--app-radius-pill);
      background: var(--app-surface-muted);
      color: var(--app-text-secondary);
      &.warn { color: var(--app-warning); background: var(--app-warning-soft); }
      &.danger { color: var(--app-danger); background: var(--app-danger-soft); }
    }
  }
}

.content {
  flex: 1;
  min-width: 0;
  padding: 20px 24px;
  box-sizing: border-box;
  @media (max-width: 768px) { padding: 16px; }
}
</style>
