import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  base: './',   // relative assets: build is prefix-agnostic (served under any /prefix/db/)
  plugins: [react(), tailwindcss()],
  server: {
    host: true,   // 👈 THIS is the key
    port: 5173
  },
})
