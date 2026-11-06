/// <reference types="vitest" />
import path from 'path';
import { mergeConfig } from 'vite';
import { defineConfig } from 'vitest/config';

import viteConfig from './vite.config';

export default mergeConfig(
  viteConfig,
  defineConfig({
    resolve: {
      alias: {
        '@chainlit/react-client': path.resolve(
          import.meta.dirname,
          '../libs/react-client/src/index.ts'
        ),
        // The workspace keeps per-package node_modules, so force the app's
        // copies to avoid loading a second React instance.
        swr: path.resolve(import.meta.dirname, './node_modules/swr'),
        zustand: path.resolve(import.meta.dirname, './node_modules/zustand')
      }
    },
    test: {
      environment: 'jsdom',
      setupFiles: './tests/setup-tests.ts',
      include: ['./**/*.{test,spec}.?(c|m)[jt]s?(x)']
    }
  })
);
