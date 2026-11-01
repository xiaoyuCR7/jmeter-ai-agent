<template>
  <div class="test-case-editor">
    <!-- Toolbar -->
    <div class="editor-toolbar">
      <div class="toolbar-left">
        <el-button @click="goBack" text :title="projectName">
          <el-icon><ArrowLeft /></el-icon> {{ t('editor.back') }}
        </el-button>
        <el-divider direction="vertical" />
        <span class="plan-name" :title="planName">{{ planName }}</span>
        <el-segmented :model-value="viewMode" :options="viewModeOptions" size="small" class="view-switch"
          :disabled="switchingView" @change="switchView" />
      </div>
      <div v-if="viewMode === 'form'" class="toolbar-center">
        <EnvironmentSelector
          :environments="envList"
          :active-env-id="activeEnvId"
          :active-env="activeEnv"
          @update:active-env-id="handleEnvChange"
          @manage="goToProjectSettings"
          @copy-env="handleCopyEnv"
        />
      </div>
      <div v-if="viewMode === 'form'" class="toolbar-right">
        <el-button @click="handleSave" :loading="saving">
          <el-icon><DocumentChecked /></el-icon> {{ t('editor.save') }}
        </el-button>
        <el-button type="primary" @click="handleRun" :loading="running">
          <el-icon><VideoPlay /></el-icon> {{ t('editor.run') }}
        </el-button>
        <el-button @click="handleViewSource">
          <el-icon><Document /></el-icon> {{ t('editor.viewJmx') }}
        </el-button>
        <el-button @click="functionHelperVisible = true">
          <span class="fx-label">fx</span> {{ t('editor.functionHelper') }}
        </el-button>
      </div>
    </div>

    <!-- Body: 3-panel layout -->
    <div v-if="viewMode === 'form'" class="editor-body">
      <!-- Left: Thread Group Config + Endpoint List -->
      <div class="left-panel">
        <div class="thread-group-config">
          <div class="config-header">
            <el-icon><User /></el-icon> {{ t('editor.threadGroup') }}
          </div>
          <p v-if="threadGroupNode" class="csv-hint">仅配置首个线程组：{{ threadGroupNode.testname }}。复杂配置请切换到 JMX 树视图。</p>
          <el-form label-position="top" size="small" class="tg-form">
            <el-row :gutter="8">
              <el-col :span="12">
                <el-form-item :label="t('editor.tg.numThreads')">
                  <el-input-number v-if="canEditNumber(threadGroupNumberValue('ThreadGroup.num_threads', 1), 1, 10000)" v-model="threadGroupConfig.numThreads" :min="1" :max="10000" style="width: 100%" />
                  <template v-else>
                    <el-input :model-value="String(threadGroupNumberValue('ThreadGroup.num_threads', 1))" readonly />
                    <span class="csv-hint">保留原始值；请切换到 JMX 树视图修改。</span>
                  </template>
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item :label="t('editor.tg.rampTime')">
                  <el-input-number v-if="canEditNumber(threadGroupNumberValue('ThreadGroup.ramp_time', 1), 0, 3600)" v-model="threadGroupConfig.rampTime" :min="0" :max="3600" style="width: 100%" />
                  <template v-else>
                    <el-input :model-value="String(threadGroupNumberValue('ThreadGroup.ramp_time', 1))" readonly />
                    <span class="csv-hint">保留原始值；请切换到 JMX 树视图修改。</span>
                  </template>
                  <span class="unit">秒</span>
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="8">
              <el-col :span="12">
                <el-form-item :label="t('editor.tg.loops')">
                  <el-input-number v-if="canEditNumber(threadGroupNumberValue('loops', 1), -1, 99999)" v-model="threadGroupConfig.loops" :min="-1" :max="99999" style="width: 100%" />
                  <template v-else>
                    <el-input :model-value="String(threadGroupNumberValue('loops', 1))" readonly />
                    <span class="csv-hint">保留原始值；请切换到 JMX 树视图修改。</span>
                  </template>
                  <span class="unit">{{ t('editor.tg.infinite') }}</span>
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item :label="t('editor.tg.onSampleError')">
                  <el-select v-model="threadGroupConfig.onSampleError" size="small" style="width: 100%">
                    <el-option :label="t('editor.tg.errorContinue')" value="continue" />
                    <el-option :label="t('editor.tg.errorStartNextLoop')" value="startnextloop" />
                    <el-option :label="t('editor.tg.errorStopThread')" value="stopthread" />
                    <el-option :label="t('editor.tg.errorStopTest')" value="stoptest" />
                    <el-option :label="t('editor.tg.errorStopTestNow')" value="stoptestnow" />
                  </el-select>
                </el-form-item>
              </el-col>
            </el-row>
            <el-form-item>
              <el-switch v-model="threadGroupConfig.scheduler" @change="handleSchedulerChange" :label="t('editor.tg.scheduler')" />
            </el-form-item>
            <el-row v-if="threadGroupConfig.scheduler" :gutter="8">
              <el-col :span="12">
                <el-form-item :label="t('editor.tg.duration')">
                  <el-input-number v-if="canEditNumber(threadGroupNumberValue('ThreadGroup.duration', 0), 1, 86400)" v-model="threadGroupConfig.duration" :min="1" :max="86400" style="width: 100%" />
                  <template v-else>
                    <el-input :model-value="String(threadGroupNumberValue('ThreadGroup.duration', 0))" readonly />
                    <span class="csv-hint">保留原始值；请切换到 JMX 树视图修改。</span>
                  </template>
                  <span class="unit">秒</span>
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item :label="t('editor.tg.delay')">
                  <el-input-number v-if="canEditNumber(threadGroupNumberValue('ThreadGroup.delay', 0), 0, 86400)" v-model="threadGroupConfig.delay" :min="0" :max="86400" style="width: 100%" />
                  <template v-else>
                    <el-input :model-value="String(threadGroupNumberValue('ThreadGroup.delay', 0))" readonly />
                    <span class="csv-hint">保留原始值；请切换到 JMX 树视图修改。</span>
                  </template>
                  <span class="unit">秒</span>
                </el-form-item>
              </el-col>
            </el-row>
          </el-form>
        </div>

        <div class="csv-dataset-config">
          <div class="config-header">
            <el-icon><Document /></el-icon> {{ t('editor.csvDataset') }}
          </div>
          <el-form label-position="top" size="small" class="csv-form">
            <el-form-item :label="t('editor.csv.filePath')">
              <el-input v-model="csvDatasetConfig.filePath" placeholder="/path/to/data.csv" size="small" />
              <div class="csv-hint">{{ t('editor.csv.fileHint') }}</div>
            </el-form-item>
            <el-form-item :label="t('editor.csv.variableNames')">
              <el-input v-model="csvDatasetConfig.variableNames" placeholder="username,password,email" size="small" />
            </el-form-item>
            <el-row :gutter="8">
              <el-col :span="12">
                <el-form-item :label="t('editor.csv.delimiter')">
                  <el-input v-model="csvDatasetConfig.delimiter" placeholder="," size="small" />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item :label="t('editor.csv.encoding')">
                  <el-select v-model="csvDatasetConfig.encoding" size="small" style="width: 100%">
                    <el-option label="UTF-8" value="UTF-8" />
                    <el-option label="GBK" value="GBK" />
                    <el-option label="ISO-8859-1" value="ISO-8859-1" />
                  </el-select>
                </el-form-item>
              </el-col>
            </el-row>
            <el-row :gutter="8">
              <el-col :span="12">
                <el-form-item :label="t('editor.csv.ignoreFirstLine')">
                  <el-switch v-model="csvDatasetConfig.ignoreFirstLine" />
                </el-form-item>
              </el-col>
              <el-col :span="12">
                <el-form-item :label="t('editor.csv.recycle')">
                  <el-switch v-model="csvDatasetConfig.recycle" />
                </el-form-item>
              </el-col>
            </el-row>
            <el-form-item :label="t('editor.csv.preview')">
              <el-button text size="small" type="primary" @click="previewCSV" :disabled="!csvDatasetConfig.filePath">
                <el-icon><View /></el-icon> {{ t('editor.csv.preview') }}
              </el-button>
            </el-form-item>
          </el-form>
          <div v-if="csvPreviewData.length > 0" class="csv-preview">
            <el-table :data="csvPreviewData" size="small" border style="width: 100%">
              <el-table-column
                v-for="(col, idx) in csvPreviewColumns"
                :key="idx"
                :label="col"
                min-width="100"
              />
            </el-table>
          </div>
        </div>

        <EndpointList
          :endpoints="endpointList"
          :selected-id="selectedEndpointId"
          @select="selectEndpoint"
          @add="addEndpoint"
          @delete="deleteEndpoint"
          @duplicate="duplicateEndpoint"
          @reorder="handleReorder"
        />
      </div>

      <!-- Center: Apifox-style main workspace -->
      <div class="center-panel">
        <div v-if="!selectedEndpoint" class="no-selection">
          <el-empty :description="t('editor.selectEndpoint')" :image-size="64" />
        </div>

        <template v-else>
          <!-- Primary view tabs (Apifox style) -->
          <div class="main-view-bar">
            <div class="main-view-tabs">
              <span
                v-for="view in mainViews"
                :key="view.name"
                :class="['main-view-tab', { active: mainView === view.name }]"
                @click="mainView = view.name"
              >
                {{ view.label }}
              </span>
            </div>
            <div class="main-view-actions">
              <el-button size="small" @click="handleSave" :loading="saving">{{ t('editor.save') }}</el-button>
              <el-button v-if="mainView === 'debug'" type="primary" size="small" @click="handleSend" :loading="sending">
                {{ t('editor.send') }}
              </el-button>
            </div>
          </div>

          <!-- Edit: API definition -->
          <div v-show="mainView === 'edit'" class="view-panel edit-view">
            <div class="edit-header">
              <el-input
                v-model="selectedEndpoint.name"
                class="endpoint-title-input"
                :placeholder="t('editor.endpointName')"
              />
              <div class="edit-meta">
                <el-tag size="small" type="warning">{{ t('editor.inDevelopment') }}</el-tag>
                <el-tag size="small">{{ selectedEndpoint.method }}</el-tag>
              </div>
            </div>
            <div class="edit-description">
              <el-input
                v-model="selectedEndpoint.description"
                type="textarea"
                :rows="3"
                :placeholder="t('editor.endpointDescription')"
              />
            </div>
            <div class="edit-request-preview">
              <div class="section-label">{{ t('editor.requestDefinition') }}</div>
              <div class="request-url-bar preview-bar">
                <el-select v-model="selectedEndpoint.method" class="method-select" size="small">
                  <el-option v-for="m in httpMethods" :key="m" :label="m" :value="m" />
                </el-select>
                <div class="url-input-group">
                  <span class="url-prefix">{{ activeEnv?.base_url || '${base_url}' }}</span>
                  <el-input v-model="selectedEndpoint.path" placeholder="/api/v1/endpoint" class="url-input" size="small" />
                </div>
              </div>
              <el-form label-position="top" size="small" class="edit-body-form">
                <el-form-item :label="t('editor.bodyType')">
                  <el-radio-group v-model="selectedEndpoint.bodyType" class="body-type-group">
                    <el-radio v-for="t in bodyTypes" :key="t.value" :value="t.value">{{ t.label }}</el-radio>
                  </el-radio-group>
                </el-form-item>
                <el-form-item v-if="selectedEndpoint.bodyType !== 'none'" :label="t('editor.bodyStructure')">
                  <el-input
                    v-model="selectedEndpoint.bodyContent"
                    type="textarea"
                    :rows="12"
                    class="body-textarea"
                    :placeholder="bodyPlaceholder"
                  />
                </el-form-item>
              </el-form>
            </div>
          </div>

          <!-- Debug: request + response split -->
          <div v-show="mainView === 'debug'" class="view-panel debug-view">
            <div class="request-url-bar">
              <el-select v-model="selectedEndpoint.method" class="method-select" size="small">
                <el-option v-for="m in httpMethods" :key="m" :label="m" :value="m" />
              </el-select>
              <div class="url-input-group">
                <span class="url-prefix">{{ activeEnv?.base_url || '${base_url}' }}</span>
                <el-input v-model="selectedEndpoint.path" placeholder="/api/v1/endpoint" class="url-input" size="small" />
              </div>
              <el-button type="primary" size="small" @click="handleSend" :loading="sending">{{ t('editor.send') }}</el-button>
            </div>

            <div class="debug-split">
              <div class="debug-request">
                <el-tabs v-model="debugTab" class="request-tabs">
                  <el-tab-pane name="params">
                    <template #label>Params <span v-if="paramCount" class="tab-badge">{{ paramCount }}</span></template>
                    <div class="tab-content">
                      <el-table :data="paramRows" border size="small">
                        <el-table-column :label="t('editor.paramName')" min-width="160">
                          <template #default="{ $index }">
                            <el-input :model-value="paramRows[$index].name" @update:model-value="updateParamRow($index, 'name', $event)" :placeholder="t('editor.paramName')" size="small" />
                          </template>
                        </el-table-column>
                        <el-table-column :label="t('editor.paramValue')" min-width="200">
                          <template #default="{ $index }">
                            <el-input :model-value="paramRows[$index].value" @update:model-value="updateParamRow($index, 'value', $event)" :placeholder="t('editor.paramValue')" size="small" />
                          </template>
                        </el-table-column>
                        <el-table-column :label="t('editor.paramDescription')" min-width="120">
                          <template #default="{ $index }">
                            <el-input :model-value="paramRows[$index].description" @update:model-value="updateParamRow($index, 'description', $event)" :placeholder="t('editor.paramDescription')" size="small" />
                          </template>
                        </el-table-column>
                        <el-table-column width="60">
                          <template #default="{ $index }">
                            <el-button text size="small" type="danger" @click="removeParamRow($index)">
                              <el-icon><Delete /></el-icon>
                            </el-button>
                          </template>
                        </el-table-column>
                      </el-table>
                      <el-button text type="primary" size="small" @click="addParamRow" style="margin-top: 6px">
                        <el-icon><Plus /></el-icon> {{ t('editor.addParam') }}
                      </el-button>
                    </div>
                  </el-tab-pane>

                  <el-tab-pane name="body">
                    <template #label>Body <span v-if="hasBody" class="tab-badge">1</span></template>
                    <div class="tab-content">
                      <el-radio-group v-model="selectedEndpoint.bodyType" class="body-type-group" size="small">
                        <el-radio v-for="t in bodyTypes" :key="t.value" :value="t.value">{{ t.label }}</el-radio>
                      </el-radio-group>
                      <el-input
                        v-if="selectedEndpoint.bodyType !== 'none'"
                        v-model="selectedEndpoint.bodyContent"
                        type="textarea"
                        :rows="10"
                        class="body-textarea"
                        :placeholder="bodyPlaceholder"
                        style="margin-top: 12px"
                      />
                    </div>
                  </el-tab-pane>

                  <el-tab-pane name="headers">
                    <template #label>Headers <span v-if="headerCount" class="tab-badge">{{ headerCount }}</span></template>
                    <div class="tab-content">
                      <HeadersEditor v-model="headerProps" />
                    </div>
                  </el-tab-pane>

                  <el-tab-pane name="cookies">
                    <template #label>Cookies <span v-if="cookieCount" class="tab-badge">{{ cookieCount }}</span></template>
                    <div class="tab-content">
                      <CookiesEditor v-model="cookieProps" />
                    </div>
                  </el-tab-pane>

                  <el-tab-pane name="auth">
                    <template #label>{{ t('editor.authType') }}</template>
                    <div class="tab-content">
                      <el-form label-position="top" size="small">
                        <el-form-item :label="t('editor.authType')">
                          <el-select v-model="selectedEndpoint.auth.type" style="width: 200px">
                            <el-option :label="t('editor.noAuth')" value="none" />
                            <el-option label="Basic Auth" value="basic" />
                            <el-option label="Bearer Token" value="bearer" />
                            <el-option label="Digest Auth" value="digest" />
                          </el-select>
                        </el-form-item>
                        <template v-if="selectedEndpoint.auth.type === 'basic' || selectedEndpoint.auth.type === 'digest'">
                          <el-row :gutter="12">
                            <el-col :span="12">
                              <el-form-item :label="t('editor.username')">
                                <el-input v-model="selectedEndpoint.auth.username" :placeholder="t('editor.username')" />
                              </el-form-item>
                            </el-col>
                            <el-col :span="12">
                              <el-form-item :label="t('editor.password')">
                                <el-input v-model="selectedEndpoint.auth.password" type="password" show-password :placeholder="t('editor.password')" />
                              </el-form-item>
                            </el-col>
                          </el-row>
                        </template>
                        <template v-if="selectedEndpoint.auth.type === 'bearer'">
                          <el-form-item label="Token">
                            <el-input v-model="selectedEndpoint.auth.password" placeholder="Bearer Token" />
                          </el-form-item>
                        </template>
                      </el-form>
                    </div>
                  </el-tab-pane>
                </el-tabs>
              </div>
              <div class="debug-response">
                <ResponsePanel :response="debugResponse" />
              </div>
            </div>
          </div>

          <!-- Test case: scripts & assertions -->
          <div v-show="mainView === 'testcase'" class="view-panel testcase-view">
            <el-tabs v-model="testcaseTab" class="request-tabs">
              <el-tab-pane :label="t('editor.preActions')" name="preScript">
                <div class="tab-content">
                  <PreProcessorEditor
                    v-model="selectedEndpoint.preScript"
                    @open-helper="functionHelperVisible = true"
                  />
                </div>
              </el-tab-pane>
              <el-tab-pane :label="t('editor.postActions')" name="postScript">
                <div class="tab-content">
                  <PostProcessorEditor
                    v-model="selectedEndpoint.postScript"
                    @add-extractor="handleAddExtractor"
                  />
                </div>
              </el-tab-pane>
              <el-tab-pane name="assertions">
                <template #label>{{ t('editor.assertions') }} <span v-if="assertionCount" class="tab-badge">{{ assertionCount }}</span></template>
                <div class="tab-content">
                  <AssertionsTab v-model="selectedEndpoint.assertions" />
                </div>
              </el-tab-pane>
            </el-tabs>
          </div>

          <!-- Settings -->
          <div v-show="mainView === 'settings'" class="view-panel settings-view">
            <div class="tab-content">
              <el-form label-position="top" size="small">
                <el-row :gutter="16">
                  <el-col :span="8">
                    <el-form-item :label="t('editor.protocol')">
                      <el-select v-model="selectedEndpoint.protocol" style="width: 100%">
                        <el-option label="" value="" />
                        <el-option label="http" value="http" />
                        <el-option label="https" value="https" />
                      </el-select>
                    </el-form-item>
                  </el-col>
                  <el-col :span="8">
                    <el-form-item :label="t('editor.server')">
                      <el-input v-model="selectedEndpoint.domain" placeholder="api.example.com" />
                    </el-form-item>
                  </el-col>
                  <el-col :span="8">
                    <el-form-item :label="t('editor.port')">
                      <el-input v-model="selectedEndpoint.port" placeholder="443" />
                    </el-form-item>
                  </el-col>
                </el-row>
                <el-row :gutter="16">
                  <el-col :span="8">
                    <el-form-item :label="t('editor.connectionTimeout')">
                      <el-input-number v-if="canEditNumber(selectedEndpoint._rawTimeout ?? selectedEndpoint.settings.timeout, 0, Number.MAX_SAFE_INTEGER)" v-model="selectedEndpoint.settings.timeout" :min="0" style="width: 100%" />
                      <template v-else>
                        <el-input :model-value="String(selectedEndpoint._rawTimeout)" readonly />
                        <span class="csv-hint">保留原始值；请切换到 JMX 树视图修改。</span>
                      </template>
                    </el-form-item>
                  </el-col>
                  <el-col :span="8">
                    <el-form-item :label="t('editor.contentEncoding')">
                      <el-input v-model="selectedEndpoint.settings.encoding" placeholder="UTF-8" />
                    </el-form-item>
                  </el-col>
                </el-row>
                <el-row :gutter="16">
                  <el-col :span="8">
                    <el-form-item :label="t('editor.followRedirects')">
                      <el-switch v-model="selectedEndpoint.settings.followRedirects" />
                    </el-form-item>
                  </el-col>
                  <el-col :span="8">
                    <el-form-item :label="t('editor.autoRedirect')">
                      <el-switch v-model="selectedEndpoint.settings.autoRedirects" />
                    </el-form-item>
                  </el-col>
                  <el-col :span="8">
                    <el-form-item label="KeepAlive">
                      <el-switch v-model="selectedEndpoint.settings.keepAlive" />
                    </el-form-item>
                  </el-col>
                </el-row>
              </el-form>
            </div>
          </div>
        </template>
      </div>
    </div>
    <JmxTreeEditor v-else ref="treeEditorRef" :plan-id="planId" class="tree-mode" />

    <!-- Function Helper Dialog -->
    <FunctionHelper v-model:visible="functionHelperVisible" @insert="handleFunctionInsert" />

    <!-- JMX Source Dialog -->
    <el-dialog v-model="sourceDialogVisible" :title="t('editor.jmxSource')" width="800px" style="max-width: calc(100vw - 32px);">
      <el-input v-model="jmxSource" type="textarea" :rows="24" readonly />
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, reactive, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  ArrowLeft, DocumentChecked, VideoPlay, Document, Plus, Delete, User, View
} from '@element-plus/icons-vue'
import { api } from '@/api'
import { useEditorStore, type EnvironmentInfo } from '@/store'
import { useI18n } from '@/composables/useI18n'
import EnvironmentSelector from '@/components/editor/EnvironmentSelector.vue'
import EndpointList from '@/components/editor/EndpointList.vue'
import PreProcessorEditor from '@/components/editor/PreProcessorEditor.vue'
import PostProcessorEditor from '@/components/editor/PostProcessorEditor.vue'
import AssertionsTab from '@/components/editor/AssertionsTab.vue'
import HeadersEditor from '@/components/editor/HeadersEditor.vue'
import CookiesEditor from '@/components/editor/CookiesEditor.vue'
import ResponsePanel from '@/components/editor/ResponsePanel.vue'
import FunctionHelper from '@/components/editor/FunctionHelper.vue'
import JmxTreeEditor from '@/components/editor/JmxTreeEditor.vue'
import type { DebugResponse } from '@/components/editor/ResponsePanel.vue'
import type { EndpointItem } from '@/components/editor/EndpointList.vue'
import type { AssertionRule } from '@/components/editor/AssertionsTab.vue'

interface EndpointData {
  id: string
  _rawTimeout?: unknown
  name: string
  description: string
  method: string
  path: string
  domain: string
  port: string
  protocol: string
  headers: Array<{ name: string; value: string }>
  cookies: Array<{ name: string; value: string; domain: string; path: string }>
  queryParams: Array<{ name: string; value: string; description: string }>
  bodyType: 'none' | 'json' | 'form-data' | 'x-www-form-urlencoded' | 'raw'
  bodyContent: string
  auth: { type: string; username: string; password: string }
  preScript: { language: string; script: string }
  postScript: { language: string; script: string }
  assertions: AssertionRule[]
  extractors: any[]
  settings: {
    timeout: number
    followRedirects: boolean
    autoRedirects: boolean
    keepAlive: boolean
    encoding: string
  }
}

const httpMethods = ['GET', 'POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'OPTIONS']
const bodyTypes = [
  { value: 'none', label: 'none' },
  { value: 'json', label: 'JSON' },
  { value: 'form-data', label: 'form-data' },
  { value: 'x-www-form-urlencoded', label: 'x-www-form-urlencoded' },
  { value: 'raw', label: 'raw' },
]

type MainView = 'edit' | 'debug' | 'testcase' | 'settings'

const route = useRoute()
const router = useRouter()
const editorStore = useEditorStore()
const { t } = useI18n()

const planId = computed(() => route.params.planId as string || route.params.id as string)
const projectId = computed(() => route.params.projectId as string)

const mainViews = computed(() => [
  { name: 'edit' as const, label: t('editor.view.edit') },
  { name: 'debug' as const, label: t('editor.view.debug') },
  { name: 'testcase' as const, label: t('editor.view.testcase') },
  { name: 'settings' as const, label: t('editor.view.settings') },
])

const planName = ref('')
const projectName = ref('')
const saving = ref(false)
const running = ref(false)
const sending = ref(false)
const mainView = ref<MainView>('edit')
const debugTab = ref('params')
const testcaseTab = ref('preScript')
const debugResponse = ref<DebugResponse | null>(null)

// Tree data (raw JMX tree)
const treeData = ref<any>(null)
const threadGroupNode = ref<any>(null)

// Endpoints parsed from tree
const endpoints = ref<Map<string, any>>(new Map())
const selectedEndpointId = ref<string | null>(null)
const selectedEndpoint = ref<EndpointData | null>(null)

// Environment
const envList = ref<EnvironmentInfo[]>([])
const activeEnvId = ref<string | null>(null)
const activeEnv = ref<EnvironmentInfo | null>(null)

// Dialogs
const functionHelperVisible = ref(false)
const sourceDialogVisible = ref(false)
const jmxSource = ref('')

// Thread Group configuration
const threadGroupConfig = reactive({
  numThreads: 1,
  rampTime: 1,
  loops: 1,
  duration: 0,
  delay: 0,
  scheduler: false,
  onSampleError: 'continue',
})

// CSV Data Set configuration
const csvDatasetConfig = reactive({
  filePath: '',
  variableNames: '',
  delimiter: ',',
  encoding: 'UTF-8',
  ignoreFirstLine: true,
  recycle: true,
})

const csvPreviewData = ref<any[]>([])
const csvPreviewColumns = ref<string[]>([])

// Computed
const endpointList = computed<EndpointItem[]>(() => {
  const result: EndpointItem[] = []
  endpoints.value.forEach((node, id) => {
    result.push({
      id,
      name: node.testname || t('common.unnamed'),
      method: node.properties?.['HTTPSampler.method'] || 'GET',
      path: node.properties?.['HTTPSampler.path'] || '/',
    })
  })
  return result
})

// Headers editor needs this format
const headerProps = computed({
  get: () => {
    const ep = selectedEndpoint.value
    if (!ep) return {}
    const list = ep.headers.map((h: any, i: number) => ({
      _elementType: 'Header',
      _name: h.name || '',
      'Header.name': h.name || '',
      'Header.value': h.value || '',
    }))
    return { 'HeaderManager.headers': list }
  },
  set: (val: any) => {
    if (!selectedEndpoint.value) return
    const headersList = val['HeaderManager.headers'] || []
    selectedEndpoint.value.headers = headersList.map((h: any) => ({
      name: h['Header.name'] || '',
      value: h['Header.value'] || '',
    }))
  },
})

// Param rows for the params table
const paramRows = computed({
  get: () => {
    if (!selectedEndpoint.value) return []
    return selectedEndpoint.value.queryParams.map((p: any) => ({ ...p }))
  },
  set: (val) => {
    if (!selectedEndpoint.value) return
    selectedEndpoint.value.queryParams = val
  },
})

const bodyPlaceholder = computed(() => {
  const type = selectedEndpoint.value?.bodyType
  if (type === 'json') return '{\n  "key": "value"\n}'
  if (type === 'raw') return t('editor.rawBodyHint')
  return t('editor.bodyHint')
})

const paramCount = computed(() =>
  selectedEndpoint.value?.queryParams.filter(p => p.name).length || 0
)
const headerCount = computed(() =>
  selectedEndpoint.value?.headers.filter(h => h.name).length || 0
)
const cookieCount = computed(() =>
  selectedEndpoint.value?.cookies?.filter(c => c.name).length || 0
)
const assertionCount = computed(() => selectedEndpoint.value?.assertions.length || 0)
const hasBody = computed(() =>
  selectedEndpoint.value?.bodyType !== 'none' && !!selectedEndpoint.value?.bodyContent
)

const cookieProps = computed({
  get: () => {
    const ep = selectedEndpoint.value
    if (!ep) return {}
    const list = (ep.cookies || []).map((c: any) => ({
      _elementType: 'Cookie',
      _name: c.name || '',
      'Cookie.name': c.name || '',
      'Cookie.value': c.value || '',
      'Cookie.domain': c.domain || '',
      'Cookie.path': c.path || '/',
    }))
    return { 'CookieManager.cookies': list }
  },
  set: (val: any) => {
    if (!selectedEndpoint.value) return
    const cookiesList = val['CookieManager.cookies'] || []
    selectedEndpoint.value.cookies = cookiesList.map((c: any) => ({
      name: c['Cookie.name'] || '',
      value: c['Cookie.value'] || '',
      domain: c['Cookie.domain'] || '',
      path: c['Cookie.path'] || '/',
    }))
  },
})

// === Form / JMX tree views ===
// Both views edit the saved plan: switching saves the current view if it changed, and the other reloads.
const viewMode = ref<'form' | 'tree'>('form')
const switchingView = ref(false)
const treeEditorRef = ref<InstanceType<typeof JmxTreeEditor> | null>(null)
const viewModeOptions = computed(() => [
  { label: t('editor.view.form'), value: 'form' },
  { label: t('editor.view.tree'), value: 'tree' },
])

async function switchView(mode: string | number | boolean) {
  if (mode === viewMode.value) return
  switchingView.value = true
  try {
    if (mode === 'tree') {
      const unchanged = JSON.stringify(buildTreeFromEndpoints()) === JSON.stringify(treeData.value)
      if (unchanged || await handleSave()) viewMode.value = 'tree'
    } else {
      const tree = treeEditorRef.value
      if (tree && tree.isDirty() && !await tree.handleSave()) return
      await loadPlan()
      viewMode.value = 'form'
    }
  } finally {
    switchingView.value = false
  }
}

// === Lifecycle ===
onMounted(async () => {
  await loadPlan()
  if (projectId.value) {
    await loadEnvironments()
    await loadProject()
  }
})

watch(selectedEndpointId, (newId) => {
  if (newId && endpoints.value.has(newId)) {
    selectedEndpoint.value = endpoints.value.get(newId)
  } else if (!newId) {
    selectedEndpoint.value = null
  }
})

// === Data Loading ===
async function loadPlan() {
  try {
    const res = await api.parseTestPlan(planId.value)
    treeData.value = res.data
    if (res.data) {
      planName.value = res.data.testname || t('editor.defaultPlanName')
      parseTreeToEndpoints(res.data)
    }
  } catch (e: any) {
    ElMessage.error(t('editor.message.loadPlanFailed', { msg: e.response?.data?.detail || e.message }))
  }
}

async function loadEnvironments() {
  try {
    const res = await api.getProjectEnvironments(projectId.value)
    if (res.data?.environments) {
      envList.value = res.data.environments
    }
    if (res.data?.active_id) {
      activeEnvId.value = res.data.active_id
      const found = envList.value.find(e => e.id === res.data.active_id)
      activeEnv.value = found || null
    } else if (envList.value.length > 0) {
      activeEnvId.value = envList.value[0].id
      activeEnv.value = envList.value[0]
    }
  } catch {
    envList.value = []
  }
}

async function loadProject() {
  try {
    const res = await api.getProject(projectId.value)
    projectName.value = res.data?.name || ''
  } catch { /* ignore */ }
}

// === Tree Parsing ===
function parseTreeToEndpoints(tree: any) {
  endpoints.value = new Map()
  threadGroupNode.value = null

  // Keep each sampler's original parent, including controllers and other groups.
  function walk(node: any, parentId?: string) {
    const tc = node.testclass
    if (!threadGroupNode.value && isThreadGroup(node)) {
      threadGroupNode.value = node
    }
    if (tc === 'HTTPSamplerProxy') {
      const endpoint = parseEndpoint(node)
      endpoint._sourceId = node.id
      endpoint._parentId = parentId
      endpoints.value.set(endpoint.id, endpoint)
    }
    if (node.children) {
      for (const child of node.children) {
        walk(child, node.id)
      }
    }
  }

  walk(tree)
  Object.assign(threadGroupConfig, readThreadGroupConfig(threadGroupNode.value))
  Object.assign(csvDatasetConfig, readCsvConfig(threadGroupNode.value?.children?.find(isCsvDataset)))

  // Select first endpoint
  if (endpoints.value.size > 0) {
    const firstKey = endpoints.value.keys().next().value as string
    if (firstKey) selectEndpoint(firstKey)
  }
}

function isThreadGroup(node: any) {
  return ['ThreadGroup', 'SetupThreadGroup', 'PostThreadGroup'].includes(node.testclass)
}

function isCsvDataset(node: any) {
  return ['CSVDataSet', 'CSVDataSetConfig'].includes(node.testclass)
}

function readThreadGroupConfig(node: any) {
  const p = node?.properties || {}
  const number = (key: string, fallback: number) => {
    const value = Number(p[key] ?? fallback)
    return Number.isFinite(value) ? value : fallback
  }
  return {
    numThreads: number('ThreadGroup.num_threads', 1), rampTime: number('ThreadGroup.ramp_time', 1),
    loops: number('loops', 1), duration: number('ThreadGroup.duration', 0), delay: number('ThreadGroup.delay', 0),
    scheduler: p['ThreadGroup.scheduler'] === true || p['ThreadGroup.scheduler'] === 'true',
    onSampleError: p['ThreadGroup.on_sample_error'] ?? 'continue',
  }
}

function threadGroupNumberValue(property: string, fallback: number): any {
  if (property === 'ThreadGroup.duration' && threadGroupConfig.duration !== readThreadGroupConfig(threadGroupNode.value).duration) {
    return threadGroupConfig.duration
  }
  const properties = threadGroupNode.value?.properties || {}
  return Object.prototype.hasOwnProperty.call(properties, property) ? properties[property] : fallback
}

function handleSchedulerChange(enabled: string | number | boolean) {
  // Only a user's switch action initializes an unset duration; mount remains lossless.
  if (enabled === true && canEditNumber(threadGroupNumberValue('ThreadGroup.duration', 0), 0, 0)) {
    threadGroupConfig.duration = 1
  }
}

function canEditNumber(value: any, min: number, max: number) {
  const number = Number(value)
  return /^-?\d+$/.test(String(value)) && Number.isSafeInteger(number) && number >= min && number <= max
}

function readCsvConfig(node: any) {
  const p = node?.properties || {}
  return { filePath: p.filename ?? '', variableNames: p.variableNames ?? '', delimiter: p.delimiter ?? ',',
    encoding: p.encoding ?? 'UTF-8', ignoreFirstLine: p.ignoreFirstLine ?? true, recycle: p.recycle ?? true }
}

function unsupportedEdit(field: string): never {
  throw new Error(`无法安全保存 ${field}；原始计划未修改。请撤销此项修改，或使用右上角 JMX 编辑器（/editor/${planId.value}）编辑复杂配置。`)
}

function parseEndpoint(node: any): any {
  const props = node.properties || {}

  // Parse headers from child HeaderManager
  const headers: Array<{ name: string; value: string }> = []
  let preScript = { language: 'groovy', script: '' }
  let postScript = { language: 'groovy', script: '' }
  const assertions: AssertionRule[] = []
  const extractors: any[] = []

  if (node.children) {
    for (const child of node.children) {
      if (child.testclass === 'HeaderManager') {
        const hlist = child.properties?.['HeaderManager.headers'] || []
        for (const h of hlist) {
          headers.push({
            name: h['Header.name'] || '',
            value: h['Header.value'] || '',
          })
        }
      }
      if (child.testclass === 'JSR223PreProcessor') {
        preScript = {
          language: child.properties?.scriptLanguage || 'groovy',
          script: child.properties?.script || '',
        }
      }
      if (child.testclass === 'JSR223PostProcessor') {
        postScript = {
          language: child.properties?.scriptLanguage || 'groovy',
          script: child.properties?.script || '',
        }
      }
      if (child.testclass === 'ResponseAssertion') {
        const testField = child.properties?.['Assertion.test_field'] || 'Assertion.response_code'
        const testType = child.properties?.['Assertion.test_type']
        assertions.push({
          type: testField.includes('response_code') ? 'status_code' : 'body_contains',
          operator: testType === 16 ? 'neq' : 'eq',
          field: '',
          expected: '',
        })
      }
      if (child.testclass === 'JSONAssertion') {
        assertions.push({
          type: 'json_path',
          operator: 'eq',
          field: child.properties?.JSON_PATH || '',
          expected: child.properties?.EXPECTED_VALUE || '',
        })
      }
      if (child.testclass === 'JSONPostProcessor' || child.testclass === 'RegexExtractor' || child.testclass === 'BoundaryExtractor') {
        extractors.push({
          type: child.testclass === 'JSONPostProcessor' ? 'JSONPath' : child.testclass === 'RegexExtractor' ? 'Regex' : 'Boundary',
          refName: child.properties?.[child.testclass === 'JSONPostProcessor' ? 'JSONPostProcessor.referenceNames' : child.testclass === 'RegexExtractor' ? 'RegexExtractor.refname' : 'BoundaryExtractor.refname'] || '',
        })
      }
    }
  }

  // Parse body
  let bodyType: EndpointData['bodyType'] = 'none'
  let bodyContent = ''
  if (props['HTTPSampler.postBodyRaw'] && props['__body_data']) {
    bodyType = 'json'
    bodyContent = props['__body_data']
  }

  return {
    id: node.id || Math.random().toString(36).substring(2, 10),
    _rawTimeout: props['HTTPSampler.connect_timeout'] || 0,
    name: node.testname || t('common.unnamed'),
    description: '',
    method: props['HTTPSampler.method'] || 'GET',
    path: props['HTTPSampler.path'] || '/',
    domain: props['HTTPSampler.domain'] || '',
    port: props['HTTPSampler.port'] || '',
    protocol: props['HTTPSampler.protocol'] || '',
    headers,
    cookies: [],
    queryParams: (props['__http_params'] || []).map((p: any) => ({
      name: p['Argument.name'] || '', value: p['Argument.value'] || '', description: '',
    })),
    bodyType,
    bodyContent,
    auth: { type: 'none', username: '', password: '' },
    preScript,
    postScript,
    assertions,
    extractors,
    settings: {
      timeout: parseInt(props['HTTPSampler.connect_timeout'] || '0'),
      followRedirects: props['HTTPSampler.follow_redirects'] !== false,
      autoRedirects: props['HTTPSampler.auto_redirects'] === true,
      keepAlive: props['HTTPSampler.use_keepalive'] !== false,
      encoding: props['HTTPSampler.contentEncoding'] || '',
    },
  }
}

// === Endpoint Actions ===
function selectEndpoint(id: string) {
  selectedEndpointId.value = id
  mainView.value = 'edit'
  debugTab.value = 'params'
}

function addEndpoint() {
  const id = Math.random().toString(36).substring(2, 10)
  endpoints.value.set(id, {
    id,
    _parentId: endpoints.value.get(selectedEndpointId.value || '')?._parentId,
    name: t('editor.newEndpoint'),
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
    settings: {
      timeout: 0,
      followRedirects: true,
      autoRedirects: false,
      keepAlive: true,
      encoding: '',
    },
  })
  selectEndpoint(id)
}

function deleteEndpoint(id: string) {
  endpoints.value.delete(id)
  if (selectedEndpointId.value === id) {
    selectedEndpointId.value = endpoints.value.keys().next().value || null
  }
}

function duplicateEndpoint(id: string) {
  const original = endpoints.value.get(id)
  if (!original) return
  const newId = Math.random().toString(36).substring(2, 10)
  const copy = JSON.parse(JSON.stringify(original))
  copy.id = newId
  copy.name = original.name + t('editor.copySuffix')
  endpoints.value.set(newId, copy)
  selectEndpoint(newId)
  ElMessage.success(t('editor.message.endpointCopied'))
}

function handleReorder(fromIndex: number, toIndex: number) {
  const entries = Array.from(endpoints.value.entries())
  const movedParent = entries[fromIndex]?.[1]._parentId
  if (entries.slice(Math.min(fromIndex, toIndex), Math.max(fromIndex, toIndex) + 1).some(([, ep]) => ep._parentId !== movedParent)) {
    ElMessage.warning('无法跨线程组或控制器移动接口；请使用右上角 JMX 编辑器调整结构。')
    return
  }
  const [removed] = entries.splice(fromIndex, 1)
  entries.splice(toIndex, 0, removed)
  endpoints.value = new Map(entries)
  const newIds = Array.from(endpoints.value.keys())
  if (selectedEndpointId.value) {
    const newIndex = newIds.indexOf(selectedEndpointId.value)
    if (newIndex !== -1) {
      selectEndpoint(selectedEndpointId.value)
    }
  }
}

// === Param rows ===
function updateParamRow(index: number, field: 'name' | 'value' | 'description', value: string) {
  const row = selectedEndpoint.value?.queryParams[index]
  if (row) row[field] = value
}

function addParamRow() {
  if (!selectedEndpoint.value) return
  selectedEndpoint.value.queryParams.push({ name: '', value: '', description: '' })
}

function removeParamRow(index: number) {
  if (!selectedEndpoint.value) return
  selectedEndpoint.value.queryParams.splice(index, 1)
}

// === Handlers ===
function handleEnvChange(envId: string) {
  activeEnvId.value = envId
  const found = envList.value.find(e => e.id === envId)
  activeEnv.value = found || null
  if (projectId.value) {
    api.setActiveEnvironment(projectId.value, envId).catch(() => {})
  }
}

async function handleCopyEnv(env: any) {
  if (!projectId.value) return
  try {
    const newEnv = {
      name: env.name + t('project.copySuffix'),
      base_url: env.base_url,
      variables: env.variables || {},
      headers: env.headers || {}
    }
    await api.createEnvironment(projectId.value, newEnv)
    await loadEnvironments()
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || t('project.message.copyFailed'))
  }
}

async function previewCSV() {
  try {
    const res = await api.previewCSV(csvDatasetConfig.filePath, 5)
    if (res.data) {
      csvPreviewColumns.value = res.data.columns
      csvPreviewData.value = res.data.rows.map((row: string[]) =>
        Object.fromEntries(res.data.columns.map((column: string, index: number) => [column, row[index] ?? ''])))
    }
  } catch (e: any) {
    ElMessage.error(e.response?.data?.detail || t('editor.csv.previewError'))
  }
}

function handleAddExtractor(extractor: any) {
  if (!selectedEndpoint.value) return
  if (!selectedEndpoint.value.extractors) selectedEndpoint.value.extractors = []
  selectedEndpoint.value.extractors.push(extractor)
}

async function handleFunctionInsert(funcString: string) {
  try {
    await navigator.clipboard.writeText(funcString)
    ElMessage.success(t('editor.message.functionCopied', { func: funcString }))
  } catch {
    const input = document.createElement('input')
    input.value = funcString
    document.body.appendChild(input)
    input.select()
    document.execCommand('copy')
    document.body.removeChild(input)
    ElMessage.success(t('editor.message.functionCopied', { func: funcString }))
  }
}

async function handleSend() {
  if (!selectedEndpoint.value) return
  sending.value = true
  debugResponse.value = null
  mainView.value = 'debug'
  try {
    ElMessage.info(t('editor.message.singleEndpointNotSupported'))
  } finally {
    sending.value = false
  }
}

// === Build Tree from Endpoints ===
function buildTreeFromEndpoints(): any {
  const root = JSON.parse(JSON.stringify(treeData.value))
  if (!root) unsupportedEdit('未加载的测试计划')
  const originals = new Map<string, any>()
  const nodes = new Map<string, any>()
  const groups: any[] = []
  function index(node: any, original: any) {
    nodes.set(node.id, node)
    originals.set(node.id, original)
    if (isThreadGroup(node)) groups.push(node)
    for (const [i, child] of (node.children || []).entries()) index(child, original.children[i])
  }
  index(root, treeData.value)

  const tg = groups[0]
  if (tg) {
    patchProperties(tg, threadGroupConfig, readThreadGroupConfig(tg), {
      numThreads: 'ThreadGroup.num_threads', rampTime: 'ThreadGroup.ramp_time', loops: 'loops',
      duration: 'ThreadGroup.duration', delay: 'ThreadGroup.delay', scheduler: 'ThreadGroup.scheduler',
      onSampleError: 'ThreadGroup.on_sample_error',
    })
    const csvNodes = (tg.children || []).filter(isCsvDataset)
    if (JSON.stringify(csvDatasetConfig) !== JSON.stringify(readCsvConfig(csvNodes[0]))) {
      if (csvNodes.length > 1) unsupportedEdit('多个 CSV 数据集')
      if (!csvDatasetConfig.filePath) {
        tg.children = tg.children.filter((child: any) => child !== csvNodes[0])
      } else {
        const csv = csvNodes[0] || { id: Math.random().toString(36).substring(2, 10), testclass: 'CSVDataSet', testname: 'CSV Data Set Config', enabled: true, properties: {}, children: [] }
        patchProperties(csv, csvDatasetConfig, csvNodes[0] ? readCsvConfig(csv) : {}, {
          filePath: 'filename', variableNames: 'variableNames', delimiter: 'delimiter', encoding: 'encoding',
          ignoreFirstLine: 'ignoreFirstLine', recycle: 'recycle',
        })
        if (!csvNodes.length) (tg.children ||= []).push(csv)
      }
    }
  } else if (JSON.stringify(threadGroupConfig) !== JSON.stringify(readThreadGroupConfig(null)) ||
    JSON.stringify(csvDatasetConfig) !== JSON.stringify(readCsvConfig(null))) {
    unsupportedEdit('缺少线程组的配置')
  }

  // Modify existing samplers in place. Never rebuild or flatten their subtrees.
  function patchChildren(node: any) {
    if (!node.children) return
    node.children = node.children.filter((child: any) => child.testclass !== 'HTTPSamplerProxy' || endpoints.value.has(child.id))
    for (const child of node.children) {
      if (child.testclass === 'HTTPSamplerProxy') patchSamplerNode(child, endpoints.value.get(child.id), originals.get(child.id))
      patchChildren(child)
    }
  }
  patchChildren(root)

  for (const ep of endpoints.value.values()) {
    if (nodes.has(ep.id)) continue
    const parent = nodes.get(ep._parentId) || (groups.length === 1 ? tg : null)
    if (!parent) unsupportedEdit('新增接口的所属线程组；请先选择同组接口')
    const original = originals.get(ep._sourceId)
    let sampler: any
    if (original) {
      sampler = JSON.parse(JSON.stringify(original))
      const renewIds = (node: any) => {
        node.id = Math.random().toString(36).substring(2, 10)
        for (const child of node.children || []) {
          if (child.testclass === 'HTTPSamplerProxy') unsupportedEdit('包含嵌套请求的接口复制')
          renewIds(child)
        }
      }
      renewIds(sampler)
      sampler.id = ep.id
      patchSamplerNode(sampler, ep, original)
    } else {
      sampler = buildSamplerNode(ep)
      // The simple builder cannot represent these fields; don't silently ignore them.
      const empty = parseEndpoint({ id: ep.id, properties: {}, children: [] })
      for (const field of ['queryParams', 'cookies', 'auth', 'assertions', 'extractors', 'description']) {
        if (JSON.stringify(ep[field]) !== JSON.stringify(empty[field])) unsupportedEdit(field)
      }
      if (['form-data', 'x-www-form-urlencoded'].includes(ep.bodyType)) unsupportedEdit('表单请求体')
    }
    (parent.children ||= []).push(sampler)
  }
  const order = new Map(Array.from(endpoints.value.keys()).map((id, i) => [id, i]))
  function reorderChildren(node: any) {
    if (!node.children) return
    const samplers = node.children.filter((child: any) => child.testclass === 'HTTPSamplerProxy')
      .sort((a: any, b: any) => (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0))
    node.children = node.children.map((child: any) => child.testclass === 'HTTPSamplerProxy' ? samplers.shift() : child)
    for (const child of node.children) reorderChildren(child)
  }
  reorderChildren(root)
  return root
}

function patchProperties(node: any, current: any, previous: any, fields: Record<string, string>) {
  for (const [field, property] of Object.entries(fields)) {
    if (JSON.stringify(current[field]) !== JSON.stringify(previous[field])) {
      (node.properties ||= {})[property] = current[field]
    }
  }
}

function patchSamplerNode(node: any, ep: any, original: any) {
  const previous = parseEndpoint(original)
  // These UI models omit JMeter options. Retain them unless edited; then require the full editor.
  for (const field of ['queryParams', 'cookies', 'auth', 'assertions', 'extractors', 'description']) {
    if (JSON.stringify(ep[field]) !== JSON.stringify(previous[field])) unsupportedEdit(field)
  }
  if (ep.name !== previous.name) node.testname = ep.name
  patchProperties(node, ep, previous, { method: 'HTTPSampler.method', path: 'HTTPSampler.path', domain: 'HTTPSampler.domain', port: 'HTTPSampler.port', protocol: 'HTTPSampler.protocol' })
  patchProperties(node, ep.settings, previous.settings, {
    timeout: 'HTTPSampler.connect_timeout', followRedirects: 'HTTPSampler.follow_redirects',
    autoRedirects: 'HTTPSampler.auto_redirects', keepAlive: 'HTTPSampler.use_keepalive', encoding: 'HTTPSampler.contentEncoding',
  })
  if (ep.bodyType !== previous.bodyType || ep.bodyContent !== previous.bodyContent) {
    if (original.properties?.__http_params?.length || original.properties?.['HTTPsampler.Arguments'] ||
      ['form-data', 'x-www-form-urlencoded'].includes(ep.bodyType)) unsupportedEdit('参数/表单请求体转换')
    if (ep.bodyType !== previous.bodyType) {
      node.properties['HTTPSampler.postBodyRaw'] = ep.bodyType !== 'none'
      if ('HTTPSampler.DO_MULTIPART_POST' in node.properties) node.properties['HTTPSampler.DO_MULTIPART_POST'] = false
    }
    if (ep.bodyType === 'none') delete node.properties.__body_data
    else node.properties.__body_data = ep.bodyContent
  }
  for (const [field, testclass] of [['headers', 'HeaderManager'], ['preScript', 'JSR223PreProcessor'], ['postScript', 'JSR223PostProcessor']]) {
    if (JSON.stringify(ep[field]) === JSON.stringify(previous[field])) continue
    const matches = (node.children || []).filter((child: any) => child.testclass === testclass)
    if (matches.length > 1) unsupportedEdit(`多个 ${testclass}`)
    let child = matches[0]
    if (!child) {
      child = { id: Math.random().toString(36).substring(2, 10), testclass, testname: testclass, enabled: true, properties: {}, children: [] }
      node.children ||= []
      node.children.push(child)
    }
    const properties = child.properties ||= {}
    if (field === 'headers') {
      const oldHeaders = properties['HeaderManager.headers'] || []
      if (oldHeaders.length !== ep.headers.length && oldHeaders.length) unsupportedEdit('请求头集合增删')
      properties['HeaderManager.headers'] = ep.headers.map((h: any, i: number) => ({
        ...(oldHeaders[i] || { _elementType: 'Header', _name: h.name }), 'Header.name': h.name, 'Header.value': h.value,
      }))
    } else {
      if (properties.filename) unsupportedEdit('外部文件脚本')
      patchProperties(child, ep[field], matches.length ? previous[field] : {}, { language: 'scriptLanguage', script: 'script' })
    }
  }
}

function buildSamplerNode(ep: any): any {
  const props: Record<string, any> = {
    'HTTPSampler.method': ep.method,
    'HTTPSampler.path': ep.path,
    'HTTPSampler.domain': ep.domain,
    'HTTPSampler.port': ep.port,
    'HTTPSampler.protocol': ep.protocol,
    'HTTPSampler.contentEncoding': ep.settings?.encoding || '',
    'HTTPSampler.follow_redirects': ep.settings?.followRedirects !== false,
    'HTTPSampler.auto_redirects': ep.settings?.autoRedirects === true,
    'HTTPSampler.use_keepalive': ep.settings?.keepAlive !== false,
    'HTTPSampler.connect_timeout': String(ep.settings?.timeout || ''),
    'HTTPSampler.postBodyRaw': ep.bodyType !== 'none',
    'HTTPSampler.DO_MULTIPART_POST': ep.bodyType === 'form-data',
  }

  if (ep.bodyType !== 'none' && ep.bodyContent) {
    props['__body_data'] = ep.bodyContent
  }

  const children: any[] = []

  // Headers
  if (ep.headers && ep.headers.length > 0) {
    children.push({
      id: Math.random().toString(36).substring(2, 10),
      testclass: 'HeaderManager',
      testname: 'HTTP Header Manager',
      enabled: true,
      properties: {
        'HeaderManager.headers': ep.headers.map((h: any) => ({
          _elementType: 'Header',
          _name: h.name || '',
          'Header.name': h.name || '',
          'Header.value': h.value || '',
        })),
      },
      children: [],
    })
  }

  // Pre-processor script
  if (ep.preScript && ep.preScript.script) {
    children.push({
      id: Math.random().toString(36).substring(2, 10),
      testclass: 'JSR223PreProcessor',
      testname: 'PreProcessor',
      enabled: true,
      properties: {
        scriptLanguage: ep.preScript.language || 'groovy',
        script: ep.preScript.script,
        parameters: '',
        filename: '',
      },
      children: [],
    })
  }

  // Post-processor script
  if (ep.postScript && ep.postScript.script) {
    children.push({
      id: Math.random().toString(36).substring(2, 10),
      testclass: 'JSR223PostProcessor',
      testname: 'PostProcessor',
      enabled: true,
      properties: {
        scriptLanguage: ep.postScript.language || 'groovy',
        script: ep.postScript.script,
        parameters: '',
        filename: '',
      },
      children: [],
    })
  }

  // Assertions
  if (ep.assertions) {
    for (const a of ep.assertions) {
      if (a.type === 'json_path') {
        children.push({
          id: Math.random().toString(36).substring(2, 10),
          testclass: 'JSONAssertion',
          testname: 'JSON Assertion',
          enabled: true,
          properties: {
            JSON_PATH: a.field || '',
            EXPECTED_VALUE: a.expected || '',
            JSONVALIDATION: false,
            EXPECT_NULL: false,
            INVERT: a.operator === 'neq',
            ISREGEX: a.operator === 'regex',
          },
          children: [],
        })
      } else if (a.type === 'duration') {
        children.push({
          id: Math.random().toString(36).substring(2, 10),
          testclass: 'DurationAssertion',
          testname: 'Duration Assertion',
          enabled: true,
          properties: {
            'DurationAssertion.duration': String(a.expected || '1000'),
          },
          children: [],
        })
      } else if (a.type === 'size') {
        const operatorMap: Record<string, number> = { eq: 1, neq: 2, gt: 3, lt: 4, gte: 5, lte: 6 }
        children.push({
          id: Math.random().toString(36).substring(2, 10),
          testclass: 'SizeAssertion',
          testname: 'Size Assertion',
          enabled: true,
          properties: {
            'SizeAssertion.size': String(a.expected || '1024'),
            'SizeAssertion.operator': operatorMap[a.operator] || 3,
          },
          children: [],
        })
      } else if (a.type === 'jsr223') {
        children.push({
          id: Math.random().toString(36).substring(2, 10),
          testclass: 'JSR223Assertion',
          testname: 'JSR223 Assertion',
          enabled: true,
          properties: {
            scriptLanguage: a.language || 'groovy',
            script: a.expected || '',
            parameters: a.parameters || '',
            filename: a.filename || '',
          },
          children: [],
        })
      } else {
        let testField = 'Assertion.response_code'
        if (a.type === 'body_contains' || a.type === 'body_regex') testField = 'Assertion.response_data'
        else if (a.type === 'header') testField = 'Assertion.response_headers'
        else if (a.type === 'response_time') testField = 'Assertion.response_data'

        let testType = 8 // Substring/contains
        if (a.operator === 'eq') testType = 4
        else if (a.operator === 'neq') testType = 16
        else if (a.operator === 'regex') testType = 2

        children.push({
          id: Math.random().toString(36).substring(2, 10),
          testclass: 'ResponseAssertion',
          testname: 'Response Assertion',
          enabled: true,
          properties: {
            'Assertion.test_field': testField,
            'Assertion.test_type': testType,
            'Assertion.assume_success': false,
            'Assertion.custom_message': '',
          },
          children: [],
        })
      }
    }
  }

  // Extractors
  if (ep.extractors) {
    for (const ext of ep.extractors) {
      if (ext.type === 'JSONPath') {
        children.push({
          id: Math.random().toString(36).substring(2, 10),
          testclass: 'JSONPostProcessor',
          testname: 'JSON Extractor',
          enabled: true,
          properties: {
            'JSONPostProcessor.referenceNames': ext.refName || '',
            'JSONPostProcessor.jsonPathExprs': ext.config?.jsonPath || '',
            'JSONPostProcessor.match_numbers': String(ext.config?.matchNo || '1'),
            'JSONPostProcessor.defaultValues': ext.config?.defaultValue || 'NOT_FOUND',
          },
          children: [],
        })
      } else if (ext.type === 'Regex') {
        children.push({
          id: Math.random().toString(36).substring(2, 10),
          testclass: 'RegexExtractor',
          testname: 'Regex Extractor',
          enabled: true,
          properties: {
            'RegexExtractor.useHeaders': false,
            'RegexExtractor.refname': ext.refName || '',
            'RegexExtractor.regex': ext.config?.regex || '',
            'RegexExtractor.template': ext.config?.template || '$1$',
            'RegexExtractor.match_number': String(ext.config?.matchNo || '1'),
            'RegexExtractor.default': ext.config?.defaultValue || 'NOT_FOUND',
          },
          children: [],
        })
      } else if (ext.type === 'Boundary') {
        children.push({
          id: Math.random().toString(36).substring(2, 10),
          testclass: 'BoundaryExtractor',
          testname: 'Boundary Extractor',
          enabled: true,
          properties: {
            'BoundaryExtractor.useHeaders': false,
            'BoundaryExtractor.refname': ext.refName || '',
            'BoundaryExtractor.lboundary': ext.config?.left || '',
            'BoundaryExtractor.rboundary': ext.config?.right || '',
            'BoundaryExtractor.match_number': String(ext.config?.matchNo || '1'),
            'BoundaryExtractor.default': ext.config?.defaultValue || 'NOT_FOUND',
          },
          children: [],
        })
      } else if (ext.type === 'XPath') {
        children.push({
          id: Math.random().toString(36).substring(2, 10),
          testclass: 'XPath2Extractor',
          testname: 'XPath Extractor',
          enabled: true,
          properties: {
            'XPath2Extractor.refname': ext.refName || '',
            'XPath2Extractor.xpathQuery': ext.config?.xpathQuery || '',
            'XPath2Extractor.matchNumber': String(ext.config?.matchNo || '1'),
            'XPath2Extractor.defaultValue': ext.config?.defaultValue || 'NOT_FOUND',
            'XPath2Extractor.namespaces': ext.config?.namespaces || '',
          },
          children: [],
        })
      }
    }
  }

  return {
    id: ep.id,
    testclass: 'HTTPSamplerProxy',
    testname: ep.name,
    enabled: true,
    properties: props,
    children,
  }
}

// === Save / Run ===
async function handleSave() {
  saving.value = true
  try {
    const tree = buildTreeFromEndpoints()
    await api.saveTestPlanTree(planId.value, { tree })
    // Refresh local state
    treeData.value = tree
    ElMessage.success(t('editor.message.saveSuccess'))
    return true
  } catch (e: any) {
    ElMessage.error(t('editor.message.saveFailed', { msg: e.response?.data?.detail || e.message }))
    return false
  } finally {
    saving.value = false
  }
}

async function handleRun() {
  running.value = true
  try {
    if (!await handleSave()) return
    await api.executeTest(planId.value)
    ElMessage.success(t('editor.message.runStarted'))
    router.push('/executions')
  } catch (e: any) {
    ElMessage.error(t('editor.message.runFailed', { msg: e.response?.data?.detail || e.message }))
  } finally {
    running.value = false
  }
}

async function handleViewSource() {
  try {
    const res = await api.getJmxContent(planId.value)
    jmxSource.value = res.data?.content || ''
    sourceDialogVisible.value = true
  } catch (e: any) {
    ElMessage.error(t('editor.message.loadJmxFailed', { msg: e.response?.data?.detail || e.message }))
  }
}

function goBack() {
  if (projectId.value) {
    router.push(`/projects/${projectId.value}/plans`)
  } else {
    router.push('/projects')
  }
}

function goToProjectSettings() {
  if (projectId.value) {
    router.push(`/projects/${projectId.value}/settings?section=environments`)
  }
}
</script>

<style scoped lang="scss">
// Full-bleed under the 52px top bar: cancel the layout's content padding (20px 24px, 16px on phones).
.test-case-editor {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 52px);
  min-height: 480px;
  margin: -20px -24px;

  @media (max-width: 768px) {
    margin: -16px;
  }
}

.view-switch { margin-left: 8px; }
.tree-mode { flex: 1; min-height: 0; }

.editor-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  border-bottom: 1px solid var(--app-border);
  background: var(--app-surface);
  flex-shrink: 0;
  flex-wrap: wrap;
  gap: 12px;

  // One row on desktop: the plan name gives way first.
  @media (min-width: 1024px) {
    flex-wrap: nowrap;
  }

  .toolbar-left {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
    flex: 1 1 auto;

    .view-switch { flex-shrink: 0; }

    .plan-name {
      font-size: 15px;
      font-weight: 500;
      color: var(--app-text);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      min-width: 0;
    }
  }
  .toolbar-center { display: flex; align-items: center; min-width: 0; flex-shrink: 0; }
  .toolbar-right {
    display: flex;
    flex-shrink: 0;
    gap: 8px;
    flex-wrap: wrap;
    min-width: 0;

    .fx-label { font-family: monospace; font-weight: bold; }
  }
}

.editor-body {
  display: flex;
  flex: 1;
  overflow: hidden;

  @media (max-width: 768px) {
    flex-direction: column;
    overflow: auto;
  }
}

.left-panel {
  width: 260px;
  min-width: 220px;
  max-width: 35%;
  flex-shrink: 0;
  border-right: 1px solid var(--app-border);
  background: var(--app-surface);
  display: flex;
  flex-direction: column;
  overflow: hidden;

  @media (max-width: 768px) {
    width: 100%;
    max-width: none;
    border-right: none;
    border-bottom: 1px solid var(--app-border);
  }
}

.center-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--app-bg);
  min-width: 0;

  .no-selection {
    display: flex;
    align-items: center;
    justify-content: center;
    flex: 1;
  }

  .main-view-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 16px;
    background: var(--app-surface);
    border-bottom: 1px solid var(--app-border);
    flex-shrink: 0;
    min-height: 44px;
    flex-wrap: wrap;
    gap: 8px;

    .main-view-tabs {
      display: flex;
      gap: 4px;
      flex-wrap: wrap;
      overflow-x: auto;
      min-width: 0;

      .main-view-tab {
        padding: 12px 16px;
        font-size: 14px;
        color: var(--app-text-secondary);
        cursor: pointer;
        border-bottom: 2px solid transparent;
        transition: all 0.15s;
        user-select: none;
        white-space: nowrap;

        @media (max-width: 768px) {
          padding: 10px 12px;
          font-size: 13px;
        }

        &:hover { color: var(--app-primary); }

        &.active {
          color: var(--app-primary);
          border-bottom-color: var(--app-primary);
          font-weight: 500;
        }
      }
    }

    .main-view-actions {
      display: flex;
      gap: 8px;
      min-width: 0;

      .el-button { width: auto; }
    }
  }

  .view-panel {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    min-height: 0;
  }

  .edit-view {
    overflow-y: auto;
    padding: 20px 24px;

    .edit-header {
      margin-bottom: 16px;

      .endpoint-title-input {
        width: 100%;
        min-width: 0;

        :deep(.el-input__wrapper) {
          box-shadow: none;
          padding: 0;
          font-size: 22px;
          font-weight: 500;

          @media (max-width: 768px) {
            font-size: 18px;
          }
        }
        :deep(.el-input__inner) {
          font-size: 22px;
          font-weight: 500;
          height: 36px;

          @media (max-width: 768px) {
            font-size: 18px;
            height: 32px;
          }
        }
      }

      .edit-meta {
        display: flex;
        gap: 8px;
        margin-top: 10px;
      }
    }

    .edit-description {
      margin-bottom: 20px;
    }

    .edit-request-preview {
      .section-label {
        font-size: 13px;
        font-weight: 500;
        color: var(--app-text-secondary);
        margin-bottom: 12px;
      }
    }

    .preview-bar {
      margin-bottom: 16px;
    }
  }

  .debug-view {
    .debug-split {
      flex: 1;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      min-height: 0;

      .debug-request {
        flex: 1;
        min-height: 0;
        overflow: hidden;
        display: flex;
        flex-direction: column;
      }

      .debug-response {
        height: 240px;
        min-height: 180px;
        max-height: 40vh;
        flex-shrink: 0;
      }
    }
  }

  .testcase-view,
  .settings-view {
    overflow: hidden;
  }

  .request-url-bar {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    padding: 12px 16px;
    background: var(--app-surface);
    border-bottom: 1px solid var(--app-border);
    flex-shrink: 0;

    .method-select {
      width: 100px;
      min-width: 100px;
      :deep(.el-input__wrapper) {
        border-radius: var(--app-radius) 0 0 var(--app-radius);
        border-right: none;
      }
    }

    .url-input-group {
      flex: 1;
      display: flex;
      align-items: center;
      border: 1px solid var(--el-border-color);
      border-radius: 0 var(--app-radius) var(--app-radius) 0;
      overflow: hidden;
      height: 32px;
      min-width: 180px;

      .url-prefix {
        font-size: 12px;
        color: var(--app-text-muted);
        padding: 0 10px;
        background: var(--app-surface);
        border-right: 1px solid var(--app-border);
        line-height: 32px;
        white-space: nowrap;
        max-width: 180px;
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .url-input {
        flex: 1;
        min-width: 120px;
        :deep(.el-input__wrapper) {
          box-shadow: none;
          border-radius: 0;
        }
      }
    }

    .el-button { margin-left: 0; width: auto; border-radius: var(--app-radius); }
  }

  .request-tabs {
    flex: 1;
    display: flex;
    flex-direction: column;
    overflow: hidden;

    :deep(.el-tabs__header) {
      margin: 0;
      padding: 0 16px;
      background: var(--app-surface);
      border-bottom: 1px solid var(--app-border);
    }

    :deep(.el-tabs__nav-wrap::after) { display: none; }

    :deep(.el-tabs__content) {
      flex: 1;
      overflow-y: auto;
      padding: 0;
    }
  }

  .tab-content {
    padding: 16px 20px;

    @media (max-width: 768px) {
      padding: 12px 14px;
    }
  }

  .tab-badge {
    display: inline-block;
    margin-left: 4px;
    padding: 0 6px;
    font-size: 11px;
    line-height: 18px;
    border-radius: var(--app-radius);
    background: var(--app-primary-soft);
    color: var(--app-primary);
    font-weight: 500;
  }

  .body-type-group {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;

    :deep(.el-radio) { margin-right: 0; }
  }
}

.body-textarea {
  :deep(.el-textarea__inner) {
    font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
    font-size: 13px;
    background: var(--app-surface);
    color: var(--app-text);
  }
}

.thread-group-config,
.csv-dataset-config {
  padding: 12px;
  border-bottom: 1px solid var(--app-border);

  .config-header {
    font-size: 13px;
    font-weight: 500;
    color: var(--app-text);
    margin-bottom: 10px;
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .tg-form,
  .csv-form {
    .el-form-item {
      margin-bottom: 8px;
    }

    .el-form-item__label {
      font-size: 12px;
      padding-bottom: 4px;
    }
  }

  .csv-hint {
    font-size: 11px;
    color: var(--app-text-secondary);
    margin-top: 4px;
  }
}

.csv-preview {
  margin-top: 12px;
  :deep(.el-table) {
    font-size: 12px;
  }
}
</style>
