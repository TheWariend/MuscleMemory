import {defineConfig} from 'vite';

const api = process.env.MM_API_URL ?? 'http://127.0.0.1:8080';

export default defineConfig({
  base: '/dashboard/',
  server: {proxy: {'/v1': api, '/health': api}},
});

