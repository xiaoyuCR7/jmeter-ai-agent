<template>
  <el-card>
    <template #header>
      <span>{{ t('accessLink.title') }}</span>
    </template>
    <p class="intro">{{ t('accessLink.intro') }}</p>
    <div class="link-row">
      <code class="link">{{ visible ? link : maskedLink }}</code>
      <div class="button-row">
        <el-button :disabled="!token" @click="visible = !visible">
          {{ visible ? t('accessLink.hide') : t('accessLink.show') }}
        </el-button>
        <el-button type="primary" :disabled="!token" @click="copy">
          {{ copied ? t('accessLink.copied') : t('accessLink.copy') }}
        </el-button>
      </div>
    </div>
    <el-alert v-if="loadFailed" type="error" :closable="false" :title="t('accessLink.loadFailed')" />
    <p class="hint">{{ t('accessLink.warning') }}</p>
  </el-card>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '@/api'
import { useI18n } from '@/composables/useI18n'

const { t } = useI18n()
const token = ref('')
const visible = ref(false)
const copied = ref(false)
const loadFailed = ref(false)

const link = computed(() => `${location.origin}/#token=${token.value}`)
const maskedLink = computed(() => `${location.origin}/#token=${'•'.repeat(12)}`)

onMounted(async () => {
  try {
    token.value = (await api.instance.get('/session/token')).data.token
  } catch {
    loadFailed.value = true
  }
})

async function copy() {
  try {
    await navigator.clipboard.writeText(link.value)
    copied.value = true
    setTimeout(() => { copied.value = false }, 2000)
  } catch {
    visible.value = true  // The browser refused: show the link so it can be copied by hand.
    ElMessage.warning(t('accessLink.copyFailed'))
  }
}
</script>

<style scoped lang="scss">
.intro { margin: 0 0 12px; color: var(--app-text-secondary); }

.link-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.link {
  flex: 1 1 280px;
  min-width: 0;
  padding: 7px 12px;
  border-radius: var(--app-radius);
  background: var(--app-surface-muted);
  color: var(--app-text);
  font-family: var(--app-font-mono);
  font-size: 13px;
  overflow-wrap: anywhere;
}
</style>
