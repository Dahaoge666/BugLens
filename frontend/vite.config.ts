import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/v1': 'http://localhost:8000',
    },
    watch: {
      // /mnt/d (Windows drive mounted in WSL2) breaks inotify; poll so HMR works.
      usePolling: true,
      interval: 100,
    },
  },
})
