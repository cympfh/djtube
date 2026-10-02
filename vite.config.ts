import { defineConfig } from 'vitest/config';

// Hosted at https://s.cympfh.cc/djtube (a path, not the site root).
export default defineConfig({
  base: '/djtube/',
  server: {
    host: '0.0.0.0',
    port: 5173,
  },
  preview: {
    host: '0.0.0.0',
    port: 4173,
  },
  test: {
    environment: 'node',
  },
});
