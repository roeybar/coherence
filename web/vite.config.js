import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// ENGINE_URL lets `npm run dev` proxy /api to a running engine
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { '/api': process.env.ENGINE_URL || 'http://localhost:8000' },
  },
})
