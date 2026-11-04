import react from '@vitejs/plugin-react-swc';
import path from 'path';
import { defineConfig } from 'vite';
import svgr from 'vite-plugin-svgr';

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react(), svgr()],
  build: {
    rollupOptions: {
      input: {
        copilot: path.resolve(import.meta.dirname, 'index.tsx')
      },
      output: [
        {
          name: 'copilot',
          dir: 'dist',
          format: 'iife',
          entryFileNames: 'index.js'
        }
      ]
    }
  },
  resolve: {
    tsconfigPaths: true,
    alias: {
      // To prevent conflicts with packages in @chainlit/app, we need to specify the resolution paths for these dependencies.
      react: path.resolve(import.meta.dirname, './node_modules/react'),
      '@chainlit': path.resolve(
        import.meta.dirname,
        './node_modules/@chainlit'
      ),
      postcss: path.resolve(import.meta.dirname, './node_modules/postcss'),
      tailwindcss: path.resolve(
        import.meta.dirname,
        './node_modules/tailwindcss'
      ),
      i18next: path.resolve(import.meta.dirname, './node_modules/i18next'),
      sonner: path.resolve(import.meta.dirname, './node_modules/sonner'),
      'highlight.js': path.resolve(
        import.meta.dirname,
        './node_modules/highlight.js'
      ),
      'react-i18next': path.resolve(
        import.meta.dirname,
        './node_modules/react-i18next'
      ),
      'usehooks-ts': path.resolve(
        import.meta.dirname,
        './node_modules/usehooks-ts'
      ),
      lodash: path.resolve(import.meta.dirname, './node_modules/lodash')
    }
  }
});
