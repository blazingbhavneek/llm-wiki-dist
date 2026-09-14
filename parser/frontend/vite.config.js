import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import tailwindcss from '@tailwindcss/vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    // FastAPI serves everything under static/ (see server.py).
    outDir: '../static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      // Dev server: forward API calls to uvicorn on :8000.
      '/parse': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
    },
  },
})
