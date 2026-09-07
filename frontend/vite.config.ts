import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/v1': 'http://127.0.0.1:8000',
    },
    watch: {
      // /mnt/d (Windows drive mounted in WSL2) breaks inotify; poll so HMR works.
      usePolling: true,
      interval: 100,
    },
  },
})
