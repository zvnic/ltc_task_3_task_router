import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    proxy: { '/api': 'http://api:8000' },
  },
  build: {
    chunkSizeWarningLimit: 1_100,
    rollupOptions: {
      output: {
        manualChunks: {
          maplibre: ['maplibre-gl'],
          tanstack: ['@tanstack/react-query', '@tanstack/react-router', '@tanstack/react-table'],
        },
      },
    },
  },
  test: { include: ['src/**/*.test.{ts,tsx}'] },
})
