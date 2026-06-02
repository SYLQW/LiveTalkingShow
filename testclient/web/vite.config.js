import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'node:path';

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'index.html'),
        motion: resolve(__dirname, 'motion.html')
      }
    }
  },
  server: {
    watch: {
      usePolling: true,
      interval: 1000
    }
  }
});
