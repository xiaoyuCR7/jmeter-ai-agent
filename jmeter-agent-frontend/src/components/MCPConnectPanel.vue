<template>
  <div class="mcp-page">
    <header class="server">
      <div class="server-main">
        <h3 class="server-title">
          <span class="dot" :class="serverState" aria-hidden="true"></span>{{ t(`mcpConnect.state.${serverState}`) }}
        </h3>
        <p v-if="status" class="hint">{{ t('mcpConnect.summary', {
          tools: status.tools_count, resources: status.resources_count, prompts: status.prompts_count }) }}</p>
        <p v-else-if="serverState === 'offline'" class="hint">{{ t('mcpConnect.offlineHelp') }}</p>
      </div>
      <el-button :loading="testing" @click="testConnection">{{ t('mcpConnect.testConnection') }}</el-button>
    </header>

    <section v-if="serverState === 'online'" class="panel setup">
      <h3>{{ t('mcpConnect.chooseClient') }}</h3>
      <div class="client-picker">
        <button v-for="client in clients" :key="client.key" type="button" class="client"
                :class="{ active: client.key === activeTool }" :aria-pressed="client.key === activeTool"
                @click="activeTool = client.key">{{ client.label }}</button>
      </div>

      <div v-for="block in currentClient.blocks" :key="block.tool" class="step">
        <p class="step-hint">{{ block.hint }}</p>
        <div class="code-block">
          <div class="code-head">
            <span class="file">{{ block.file }}</span>
            <button type="button" class="code-action" @click="copy(block.tool, getConfig(block.tool))">
              <el-icon><Check v-if="copied === block.tool" /><CopyDocument v-else /></el-icon>
              {{ copied === block.tool ? t('mcpConnect.copied') : t('mcpConnect.copy') }}
            </button>
          </div>
          <pre><code>{{ shown(getConfig(block.tool)) }}</code></pre>
        </div>
      </div>
      <p v-if="currentClient.after" class="after">{{ currentClient.after }}</p>

      <div class="token-note">
        <p>{{ t('mcpConnect.tokenNote') }}</p>
        <button type="button" class="code-action" @click="showToken = !showToken">
          <el-icon><Hide v-if="showToken" /><View v-else /></el-icon>
          {{ t(showToken ? 'mcpConnect.hideToken' : 'mcpConnect.showToken') }}
        </button>
      </div>
    </section>

    <section v-if="serverState === 'online'" class="endpoint">
      <span class="endpoint-label">{{ t('mcpConnect.urlLabel') }}</span>
      <code>{{ mcpUrl }}</code>
      <button type="button" class="code-action" @click="copy('url', mcpUrl)">
        <el-icon><Check v-if="copied === 'url'" /><CopyDocument v-else /></el-icon>
        {{ copied === 'url' ? t('mcpConnect.copied') : t('mcpConnect.copyUrl') }}
      </button>
    </section>

    <section v-if="serverState === 'online'" class="after-setup">
      <h3>{{ t('mcpConnect.usageTitle') }}</h3>
      <ul>
        <li>{{ t('mcpConnect.usage.approval') }}</li>
        <li>{{ t('mcpConnect.usage.background') }}</li>
        <li>{{ t('mcpConnect.usage.analysis') }}</li>
        <li>{{ t('mcpConnect.usage.sse') }}</li>
      </ul>
    </section>

    <details v-if="status?.tools?.length" class="catalog">
      <summary>{{ t('mcpConnect.allTools', { count: status.tools.length }) }}</summary>
      <el-input v-model="toolQuery" class="tool-search" clearable :placeholder="t('mcpConnect.searchTools')" :prefix-icon="Search" />
      <div v-for="group in toolGroups" :key="group.title" class="tool-group">
        <h4>{{ group.title }}<span class="count num">{{ group.tools.length }}</span></h4>
        <dl>
          <div v-for="tool in group.tools" :key="tool.name" class="tool">
            <dt>{{ tool.name }}</dt>
            <dd>{{ tool.description }}</dd>
          </div>
        </dl>
      </div>
      <p v-if="!toolGroups.length" class="hint">{{ t('mcpConnect.noTools') }}</p>
    </details>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { api } from '@/api'
import { ElMessage } from 'element-plus'
import { Check, CopyDocument, Hide, Search, View } from '@element-plus/icons-vue'
import { useI18n } from '@/composables/useI18n'

const { t } = useI18n()

const status = ref<any>(null)
const serverState = ref<'checking' | 'online' | 'offline'>('checking')
const testing = ref(false)
const activeTool = ref('claude')
const showToken = ref(false)
const copied = ref('')
const toolQuery = ref('')
let copiedTimer: ReturnType<typeof setTimeout> | undefined

const mcpUrl = computed(() => `${location.origin}/api/v1/mcp`)
const accessToken = ref('')

const clients = computed(() => [
  { key: 'claude', label: t('mcpConnect.tools.claude'), after: t('mcpConnect.claude.hint'), blocks: [
    { tool: 'claude-cli', file: t('mcpConnect.terminal'), hint: t('mcpConnect.claude.cli') },
    { tool: 'claude', file: '.mcp.json', hint: t('mcpConnect.claude.file') },
  ] },
  { key: 'cursor', label: t('mcpConnect.tools.cursor'), after: t('mcpConnect.cursor.hint'), blocks: [
    { tool: 'cursor', file: '.cursor/mcp.json', hint: t('mcpConnect.cursor.instruction') },
  ] },
  { key: 'vscode', label: t('mcpConnect.tools.vscode'), after: '', blocks: [
    { tool: 'vscode', file: '.vscode/mcp.json', hint: t('mcpConnect.vscode.instruction') },
  ] },
  { key: 'trae', label: t('mcpConnect.tools.trae'), after: t('mcpConnect.trae.hint'), blocks: [
    { tool: 'trae', file: 'mcp.json', hint: t('mcpConnect.trae.instruction') },
  ] },
  { key: 'generic', label: t('mcpConnect.tools.generic'), after: '', blocks: [
    { tool: 'generic', file: t('mcpConnect.terminal'), hint: t('mcpConnect.generic.instruction') },
  ] },
])
const currentClient = computed(() => clients.value.find(client => client.key === activeTool.value) || clients.value[0])

const toolGroups = computed(() => {
  const query = toolQuery.value.trim().toLowerCase()
  const tools: any[] = (status.value?.tools || []).filter((tool: any) =>
    !query || `${tool.name} ${tool.description}`.toLowerCase().includes(query))
  const needsApproval = (tool: any) => tool.risk === 'write' || tool.risk === 'execute'
  return [
    { title: t('mcpConnect.needsApproval'), tools: tools.filter(needsApproval) },
    { title: t('mcpConnect.runsDirectly'), tools: tools.filter(tool => !needsApproval(tool)) },
  ].filter(group => group.tools.length)
})

async function loadStatus() {
  try {
    accessToken.value = (await api.instance.get('/session/token')).data.token
    const res = await api.instance.get('/mcp/status')
    status.value = res.data
    serverState.value = 'online'
  } catch {
    status.value = null
    serverState.value = 'offline'
  }
}

function getConfig(tool: string): string {
  if (tool === 'claude-cli') {
    return `claude mcp add --transport http jmeter-agent ${mcpUrl.value} --header "Authorization: Bearer ${accessToken.value}"`
  }
  if (tool === 'generic') {
    return `curl -s -X POST "${mcpUrl.value}" -H "Authorization: Bearer ${accessToken.value}" -H "Content-Type: application/json" \\
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1.0"}}}'

curl -s -X POST "${mcpUrl.value}" -H "Authorization: Bearer ${accessToken.value}" -H "Content-Type: application/json" \\
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'`
  }
  const entry = {
    ...(tool === 'cursor' ? {} : { type: 'http' }), url: mcpUrl.value,
    headers: { Authorization: `Bearer ${accessToken.value}` },
  }
  return JSON.stringify({ [tool === 'vscode' ? 'servers' : 'mcpServers']: { 'jmeter-agent': entry } }, null, 2)
}

// The page may be on a shared screen; only what is copied carries the real token.
const shown = (text: string) => showToken.value || !accessToken.value ? text : text.split(accessToken.value).join('•'.repeat(12))

async function copy(key: string, text: string) {
  try {
    await navigator.clipboard.writeText(text)
    copied.value = key
    clearTimeout(copiedTimer)
    copiedTimer = setTimeout(() => { copied.value = '' }, 1800)
  } catch {
    ElMessage.warning(t('mcpConnect.message.copyFailed'))
  }
}

async function testConnection() {
  testing.value = true
  try {
    const init = await api.instance.post('/mcp', {
      jsonrpc: '2.0', id: 'init', method: 'initialize',
      params: { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'web-ui', version: '1.0' } },
    })
    if (init.data.error) throw new Error(init.data.error.message)
    const ping = await api.instance.post('/mcp', { jsonrpc: '2.0', id: 'probe', method: 'ping' })
    if (ping.data.error) throw new Error(ping.data.error.message)
    await loadStatus()
    ElMessage.success(t('mcpConnect.message.connectionSuccess', init.data.result.serverInfo))
  } catch (error) {
    serverState.value = 'offline'
    ElMessage.error(t('mcpConnect.message.connectionFailed', { message: error instanceof Error ? error.message : '' }))
  } finally {
    testing.value = false
  }
}

onMounted(loadStatus)
onUnmounted(() => clearTimeout(copiedTimer))
</script>

<style scoped lang="scss">
.mcp-page {
  --code-font: var(--app-font-mono);
  min-width: 0;

  h3 { margin: 0; font-size: 15px; font-weight: 600; color: var(--app-text); }
}

.server {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 16px;
  padding: 4px 0 20px;
}

.server-main { min-width: 0; }

.server-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px !important;
}

.dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  flex-shrink: 0;
  background: var(--app-border-strong);

  &.online { background: var(--app-success); box-shadow: 0 0 0 3px var(--app-success-soft); }
  &.offline { background: var(--app-danger); box-shadow: 0 0 0 3px var(--app-danger-soft); }
}

.server .hint { margin-top: 4px; font-size: 13px; padding-left: 17px; }

.setup { padding: 20px; }

.client-picker {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  width: fit-content;
  max-width: 100%;
  margin: 12px 0 20px;
  padding: 3px;
  background: var(--app-surface-muted);
  border-radius: var(--app-radius-lg);
}

.client {
  padding: 6px 14px;
  border: 0;
  border-radius: var(--app-radius);
  background: none;
  color: var(--app-text-secondary);
  font: inherit;
  font-size: 13px;
  cursor: pointer;

  &:hover { color: var(--app-text); }

  &.active {
    background: var(--app-surface);
    color: var(--app-primary);
    font-weight: 600;
    box-shadow: var(--app-shadow);
  }
}

.client:focus-visible, .code-action:focus-visible, .catalog summary:focus-visible {
  outline: 2px solid var(--app-primary);
  outline-offset: 2px;
}

.step + .step { margin-top: 18px; }
.step-hint { margin: 0 0 8px; font-size: 13px; color: var(--app-text-secondary); line-height: 1.6; }

.code-block {
  border: 1px solid var(--app-border);
  border-radius: var(--app-radius-lg);
  background: var(--app-surface-muted);
  overflow: hidden;

  pre { margin: 0; padding: 12px 14px; max-height: 280px; overflow: auto; }

  code {
    font-family: var(--code-font);
    font-size: 12.5px;
    line-height: 1.65;
    color: var(--app-text);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
}

.code-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  padding: 5px 6px 5px 14px;
  border-bottom: 1px solid var(--app-border);
}

.file { font-size: 12px; color: var(--app-text-muted); min-width: 0; overflow-wrap: anywhere; }

.code-action {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
  padding: 4px 8px;
  border: 0;
  border-radius: var(--app-radius);
  background: none;
  color: var(--app-text-secondary);
  font: inherit;
  font-size: 12px;
  cursor: pointer;

  &:hover { background: var(--app-hover); color: var(--app-primary); }
}

.after { margin: 14px 0 0; font-size: 13px; color: var(--app-text-secondary); line-height: 1.6; }

.token-note {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  margin-top: 16px;
  padding-top: 14px;
  border-top: 1px solid var(--app-border);

  p { margin: 0; font-size: 13px; color: var(--app-text-muted); line-height: 1.6; }
}

.endpoint {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 10px;
  padding: 16px 20px;

  code { font-family: var(--code-font); font-size: 12.5px; color: var(--app-text); overflow-wrap: anywhere; min-width: 0; }
}

.endpoint-label { font-size: 13px; color: var(--app-text-muted); }

.after-setup {
  padding: 8px 20px 4px;

  ul { margin: 10px 0 0; padding-left: 18px; }

  li {
    max-width: 70ch;
    margin-bottom: 6px;
    font-size: 13px;
    line-height: 1.65;
    color: var(--app-text-secondary);
  }
}

.catalog {
  margin-top: 16px;
  padding: 14px 20px;
  border-top: 1px solid var(--app-border);

  summary {
    font-size: 14px;
    font-weight: 600;
    color: var(--app-text);
    cursor: pointer;
    border-radius: var(--app-radius);
  }
}

.tool-search { max-width: 320px; margin-top: 14px; }

.tool-group {
  margin-top: 20px;

  h4 { margin: 0; font-size: 13px; font-weight: 600; color: var(--app-text-secondary); }
  .count { margin-left: 8px; font-weight: 400; color: var(--app-text-muted); }
  dl { margin: 6px 0 0; }
}

.tool {
  display: grid;
  grid-template-columns: minmax(170px, 230px) minmax(0, 1fr);
  gap: 16px;
  padding: 8px 0;
  border-bottom: 1px solid var(--app-border);

  &:last-child { border-bottom: 0; }

  dt { font-family: var(--code-font); font-size: 12.5px; color: var(--app-text); overflow-wrap: anywhere; }
  dd { margin: 0; font-size: 13px; line-height: 1.55; color: var(--app-text-secondary); }
}

@media (max-width: 640px) {
  .server { align-items: flex-start; }
  .setup { padding: 16px; }
  .endpoint, .after-setup, .catalog { padding-left: 4px; padding-right: 4px; }
  .token-note { align-items: flex-start; flex-direction: column; gap: 6px; }
  .tool { grid-template-columns: minmax(0, 1fr); gap: 2px; }
}
</style>
