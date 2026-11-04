<template>
  <el-form label-position="top" size="small">
    <el-form-item :label="t('editor.authType')">
      <el-select v-model="auth.type" style="width: 200px">
        <el-option :label="t('editor.noAuth')" value="none" />
        <el-option label="Basic Auth" value="basic" />
        <el-option label="Bearer Token" value="bearer" />
        <el-option label="Digest Auth" value="digest" />
      </el-select>
    </el-form-item>
    <template v-if="auth.type === 'basic' || auth.type === 'digest'">
      <el-row :gutter="12">
        <el-col :span="12">
          <el-form-item :label="t('editor.username')">
            <el-input v-model="auth.username" :placeholder="t('editor.username')" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item :label="t('editor.password')">
            <el-input v-model="auth.password" type="password" show-password :placeholder="t('editor.password')" />
          </el-form-item>
        </el-col>
      </el-row>
    </template>
    <template v-if="auth.type === 'bearer'">
      <el-form-item label="Token">
        <el-input v-model="auth.password" placeholder="Bearer Token" />
      </el-form-item>
    </template>
  </el-form>
</template>

<script setup lang="ts">
import { useI18n } from '@/composables/useI18n'
import type { EndpointData } from '@/composables/usePlanTree'

// The selected endpoint's own auth object: the editor saves what is edited here.
defineProps<{ auth: EndpointData['auth'] }>()
const { t } = useI18n()
</script>
