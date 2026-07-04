import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// During `npm run dev`, Vite serves the SPA on :5173 and proxies the backend
// paths to the running aiohttp `serve` — so the browser sees a single origin (no
// CORS) and the Basic credentials flow through. Edit BACKEND if your `serve` runs
// on another host/port (config http.port default is 3000).
const BACKEND = 'http://127.0.0.1:3000'
const proxied = ['/api', '/download', '/status', '/metrics', '/ping']

export default defineConfig({
  plugins: [react()],
  base: '/',
  build: {
    // committed and served by aiohttp (src/tgshelf/http/webui.py STATIC_DIR)
    outDir: '../src/tgshelf/webui/static',
    emptyOutDir: true,
  },
  server: {
    proxy: Object.fromEntries(
      proxied.map((p) => [p, { target: BACKEND, changeOrigin: true }]),
    ),
  },
})
