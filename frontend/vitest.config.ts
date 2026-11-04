/// <reference types="vitest" />
import react from '@vitejs/plugin-react-swc';
import { defineConfig } from 'vite';

export default defineConfig({
  plugins: [react()],
  resolve: {
    tsconfigPaths: true
  },
  test: {
    environment: 'jsdom',
    setupFiles: './tests/setup-tests.ts',
    include: ['./**/*.{test,spec}.?(c|m)[jt]s?(x)']
  }
});
