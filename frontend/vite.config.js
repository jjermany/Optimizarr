import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/metrics': 'http://localhost:8000',
      '/jobs': 'http://localhost:8000',
      '/settings': 'http://localhost:8000',
    },
  },
});
