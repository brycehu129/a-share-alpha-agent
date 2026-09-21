import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// 构建产物直接写进 server/static/，由 server/webapp.py 以 /static/ 提供。
// 开发时（npm run dev）base 用 '/'，并把 /api 代理到本地 Python 服务。
export default defineConfig(({ command }) => ({
  base: command === 'build' ? '/static/' : '/',
  plugins: [
    vue(),
    AutoImport({ resolvers: [ElementPlusResolver()], dts: false }),
    Components({ resolvers: [ElementPlusResolver()], dts: false }),
  ],
  build: { outDir: '../server/static', emptyOutDir: true, chunkSizeWarningLimit: 900 },
  server: {
    proxy: { '/api': process.env.API_TARGET || 'http://127.0.0.1:8080' },
  },
  test: { environment: 'node' },
}))
