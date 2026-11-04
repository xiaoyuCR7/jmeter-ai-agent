import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

const backend = `http://localhost:${process.env.PORT || 8000}`

export default defineConfig({
  plugins: [
    vue(),
    AutoImport({
      // main.ts loads Element Plus's full stylesheet before ours. Per-component imports would load
      // after the theme and put its variables back to their defaults.
      resolvers: [ElementPlusResolver({ importStyle: false })],
      imports: ['vue', 'vue-router', 'pinia'],
      dts: false,
    }),
    Components({
      resolvers: [ElementPlusResolver({ importStyle: false })],
      dts: false,
    }),
  ],
  css: {
    preprocessorOptions: {
      scss: {
        api: 'modern-compiler',
      },
    },
  },
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    port: 3000,
    proxy: {
      '/api': {
        target: backend,
        changeOrigin: true,
      },
      '/reports': {
        target: backend,
        changeOrigin: true,
      },
    },
    hmr: {
      overlay: true,
    },
  },
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    sourcemap: false,
    minify: 'terser',
    rollupOptions: {
      output: {
        // Only libraries every page uses get a chunk of their own. Naming whole packages here
        // (element-plus, echarts) put all of each in the first download, tree shaking undone.
        manualChunks: {
          'vue-core': ['vue', 'vue-router', 'pinia'],
          'markdown': ['markdown-it', 'highlight.js'],
        },
      },
    },
    chunkSizeWarningLimit: 1000,
  },
})
