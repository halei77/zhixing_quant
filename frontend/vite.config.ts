import { fileURLToPath, URL } from 'node:url'

import tailwindcss from '@tailwindcss/vite'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 前端开发/预览把 /api 代理到本地 zx-site（ADR-0018 决定 3）。
// 生产由 FastAPI 发 frontend/dist，同源，不需要代理。
const apiProxy = { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true } }

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  resolve: {
    alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) },
  },
  server: { proxy: apiProxy },
  preview: { proxy: apiProxy },
})
