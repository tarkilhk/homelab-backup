import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        // Allow overriding backend origin during local dev.
        // Use VITE_API_ORIGIN if provided, otherwise default to FastAPI default port 8080.
        target: process.env.VITE_API_ORIGIN || 'http://localhost:8080',
        changeOrigin: true,
      },
    },
  },
})
