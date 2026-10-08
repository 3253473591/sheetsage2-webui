import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * 前端构建配置。
 *
 * 构建产物直接写进 Python 包里的 `webui/static/`，这样 `python -m sheetsage2_webui.webui`
 * 一个命令就能把后端 + 页面一起起起来；开发时用 `pnpm dev`，由 Vite 把 `/api` 代理到
 * 本机的 FastAPI（默认 127.0.0.1:8000）。
 */
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  build: {
    outDir: '../src/sheetsage2_webui/webui/static',
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      '/api': {
        target: process.env.SHEETSAGE_API ?? 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
