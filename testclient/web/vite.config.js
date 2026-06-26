import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'node:path';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const port = Number.parseInt(env.TEST_CLIENT_PORT || '', 10);
  const server = {
    host: env.TEST_CLIENT_HOST || '0.0.0.0',
    watch: {
      usePolling: true,
      interval: 1000
    }
  };
  if (Number.isFinite(port) && port > 0) {
    server.port = port;
  }

  return {
    plugins: [react()],
    build: {
      rollupOptions: {
        input: {
          main: resolve(__dirname, 'index.html'),
          motion: resolve(__dirname, 'motion.html'),
          renderLibrary: resolve(__dirname, 'render-library.html')
        }
      }
    },
    server,
    preview: {
      host: server.host,
      port: server.port
    }
  };
});
