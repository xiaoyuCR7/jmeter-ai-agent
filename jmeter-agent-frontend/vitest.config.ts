import { configDefaults, defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    globals: true,
    exclude: [...configDefaults.exclude, 'e2e/**'],
    coverage: {
      provider: 'v8',
      exclude: [
        'node_modules/',
        'src/main.ts',
        'src/App.vue',
        'src/router/**',
        'src/store/**',
        'src/api/**'
      ]
    },
    resolveSnapshotPath: (testPath, extension) => {
      return testPath.replace('src/', 'tests/snapshots/') + extension
    }
  },
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src')
    }
  }
})
