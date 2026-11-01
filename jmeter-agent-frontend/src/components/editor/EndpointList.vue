<template>
  <div class="endpoint-list">
    <div class="list-header">
      <span>{{ t('editor.endpointList.title') }}</span>
      <el-button text size="small" type="primary" @click="$emit('add')">
        <el-icon><Plus /></el-icon>
      </el-button>
    </div>

    <div class="list-body" v-if="endpoints.length > 0">
      <div
        v-for="(ep, index) in endpoints"
        :key="ep.id"
        :class="['endpoint-item', { active: selectedId === ep.id, dragging: dragIndex === index, 'drag-over': dragOverIndex === index }]"
        :draggable="true"
        @dragstart="handleDragStart($event, index)"
        @dragend="handleDragEnd"
        @dragover.prevent="handleDragOver($event, index)"
        @dragleave="handleDragLeave"
        @drop="handleDrop($event, index)"
        @click="$emit('select', ep.id)"
      >
        <div class="ep-drag-handle">
          <el-icon><Menu /></el-icon>
        </div>
        <div class="ep-left">
          <span :class="['method-badge', ep.method?.toLowerCase() || 'get']">
            {{ ep.method || 'GET' }}
          </span>
        </div>
        <div class="ep-main">
          <div class="ep-name">{{ ep.name || t('editor.endpointList.unnamed') }}</div>
          <div class="ep-path">{{ ep.path || '/' }}</div>
        </div>
        <div class="ep-actions">
          <el-dropdown trigger="click" @command="(cmd: string) => handleCommand(cmd, ep)">
            <el-button text size="small" @click.stop>
              <el-icon><MoreFilled /></el-icon>
            </el-button>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="duplicate">
                  <el-icon><CopyDocument /></el-icon> {{ t('editor.endpointList.duplicate') }}
                </el-dropdown-item>
                <el-dropdown-item command="delete" divided>
                  <el-icon><Delete /></el-icon> {{ t('editor.endpointList.delete') }}
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </div>
    </div>

    <el-empty v-else :description="t('editor.endpointList.empty')" :image-size="48">
      <el-button size="small" type="primary" @click="$emit('add')">{{ t('editor.endpointList.add') }}</el-button>
    </el-empty>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { Plus, MoreFilled, CopyDocument, Delete, Menu } from '@element-plus/icons-vue'
import { useI18n } from '@/composables/useI18n'

const { t } = useI18n()

export interface EndpointItem {
  id: string
  name: string
  method: string
  path: string
}

const props = defineProps<{
  endpoints: EndpointItem[]
  selectedId: string | null
}>()

const emit = defineEmits<{
  'select': [id: string]
  'add': []
  'delete': [id: string]
  'duplicate': [id: string]
  'reorder': [fromIndex: number, toIndex: number]
}>()

const dragIndex = ref<number | null>(null)
const dragOverIndex = ref<number | null>(null)

function handleDragStart(event: DragEvent, index: number) {
  dragIndex.value = index
  if (event.dataTransfer) {
    event.dataTransfer.effectAllowed = 'move'
    event.dataTransfer.setData('text/plain', String(index))
  }
}

function handleDragEnd() {
  dragIndex.value = null
  dragOverIndex.value = null
}

function handleDragOver(event: DragEvent, index: number) {
  if (event.dataTransfer) {
    event.dataTransfer.dropEffect = 'move'
  }
  dragOverIndex.value = index
}

function handleDragLeave() {
  dragOverIndex.value = null
}

function handleDrop(event: DragEvent, toIndex: number) {
  event.preventDefault()
  if (dragIndex.value !== null && dragIndex.value !== toIndex) {
    emit('reorder', dragIndex.value, toIndex)
  }
  dragIndex.value = null
  dragOverIndex.value = null
}

function handleCommand(cmd: string, ep: EndpointItem) {
  if (cmd === 'delete') {
    emit('delete', ep.id)
  } else if (cmd === 'duplicate') {
    emit('duplicate', ep.id)
  }
}
</script>

<style scoped lang="scss">
.endpoint-list {
  display: flex;
  flex-direction: column;
  height: 100%;

  .list-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 12px 14px;
    border-bottom: 1px solid var(--app-border);
    font-size: 13px;
    font-weight: 500;
    color: var(--app-text);
    text-transform: uppercase;
    letter-spacing: 0.03em;
  }

  .list-body {
    flex: 1;
    overflow-y: auto;
    padding: 6px;
  }

  .endpoint-item {
    display: flex;
    align-items: center;
    padding: 10px 10px;
    border-radius: var(--app-radius);
    cursor: pointer;
    transition: all 0.12s ease;
    margin-bottom: 3px;
    gap: 10px;

    &:hover {
      background: var(--app-hover);

      .ep-actions {
        opacity: 1;
      }

      .ep-drag-handle {
        opacity: 1;
      }
    }

    &.active {
      background: var(--app-primary-soft);
      box-shadow: inset 0 0 0 1px var(--app-primary);
      .ep-name { color: var(--app-primary); }
    }

    &.dragging {
      opacity: 0.5;
      cursor: grabbing;
    }

    &.drag-over {
      border: 2px dashed var(--app-primary);
      background: var(--app-primary-soft);
    }

    .ep-drag-handle {
      opacity: 0;
      cursor: grab;
      color: var(--app-text-muted);
      font-size: 14px;
      padding: 2px;
      transition: opacity 0.12s;
      flex-shrink: 0;

      &:hover {
        color: var(--app-primary);
      }

      &:active {
        cursor: grabbing;
      }
    }

    .ep-left {
      flex-shrink: 0;
    }

    .ep-main {
      flex: 1;
      min-width: 0;

      .ep-name {
        font-size: 13px;
        font-weight: 500;
        color: var(--app-text);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        margin-bottom: 2px;
      }

      .ep-path {
        font-size: 11px;
        color: var(--app-text-muted);
        font-family: var(--app-font-mono);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
    }

    .ep-actions {
      opacity: 0;
      transition: opacity 0.12s;
      flex-shrink: 0;
    }
  }

  .method-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: var(--app-radius-sm);
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.02em;
    min-width: 46px;
    text-align: center;
    font-family: var(--app-font-mono);
    // One hue per method, mixed toward the text color so it stays legible in both themes.
    color: color-mix(in srgb, var(--method) 72%, var(--app-text));
    background: color-mix(in srgb, var(--method) 14%, transparent);

    &.get { --method: #12a06f; }
    &.post { --method: #2f6fe4; }
    &.put { --method: #d9820f; }
    &.delete { --method: #dc3545; }
    &.patch { --method: #8250df; }
    &.head { --method: #0f97a8; }
    &.options { --method: #7a8699; }
  }
}
</style>
