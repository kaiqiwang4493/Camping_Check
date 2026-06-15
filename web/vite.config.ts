import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target:
          process.env.VITE_API_BASE_URL ||
          'https://camping-check-api-382568179050.us-central1.run.app',
        changeOrigin: true,
        secure: true,
      },
    },
  },
});
