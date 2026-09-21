/// <reference types="vitest/config" />
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import react from '@vitejs/plugin-react';
import { defineConfig, loadEnv, type Plugin } from 'vite';

// The mock service worker ships with msw; we serve it in dev and emit it only into demo builds,
// so production bundles never contain mocks.
const MSW_WORKER = fileURLToPath(new URL('./node_modules/msw/lib/mockServiceWorker.js', import.meta.url));

/** The mount prefix used by `npm run dev` (the Daemon injects the real one in production). */
function devBase(env: Record<string, string>): string {
  const raw = (env.CURATOR_BASE ?? '').trim().replace(/\/+$/, '');
  if (raw && !/^\/[A-Za-z0-9._~\-/]*$/.test(raw)) throw new Error(`CURATOR_BASE must look like /curation, got ${raw}`);
  return raw;
}

function curatorDevPlugin(base: string, emitWorker: boolean, injectBase: boolean): Plugin {
  return {
    name: 'curator-dev',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if ((req.url ?? '').split('?')[0] === `${base}/mockServiceWorker.js`) {
          res.setHeader('Content-Type', 'text/javascript; charset=utf-8');
          res.end(readFileSync(MSW_WORKER));
          return;
        }
        next();
      });
    },
    transformIndexHtml(html) {
      // Mimic the Daemon (doc 07 §2.3): inject the mount prefix before the base bootstrap script.
      if (!injectBase) return html;
      return html.replace('<!-- curator:base -->', `<script>window.__CURATOR_BASE__ = ${JSON.stringify(base)};</script>`);
    },
    generateBundle() {
      if (emitWorker) {
        this.emitFile({ type: 'asset', fileName: 'mockServiceWorker.js', source: readFileSync(MSW_WORKER, 'utf8') });
      }
    },
  };
}

export default defineConfig(({ command, mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const base = devBase(env);
  const apiTarget = (env.VITE_API_TARGET ?? '').trim();
  const isDev = command === 'serve';
  const mocks = mode === 'test' ? false : isDev ? !apiTarget : mode === 'demo';

  return {
    // Relative asset paths: the same build works under any mount prefix (doc 07 §2.3).
    base: isDev ? `${base}/` : './',
    plugins: [react(), curatorDevPlugin(base, mode === 'demo', isDev)],
    define: {
      __CURATOR_MOCKS__: JSON.stringify(mocks),
    },
    server: {
      port: 5173,
      proxy: apiTarget
        ? {
            [`${base}/api`]: { target: apiTarget, changeOrigin: true },
            [`${base}/events`]: { target: apiTarget, changeOrigin: true },
          }
        : undefined,
    },
    build: {
      outDir: 'dist',
      sourcemap: false,
      chunkSizeWarningLimit: 1200,
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (id.includes('node_modules/echarts') || id.includes('node_modules/zrender')) return 'echarts';
            if (id.includes('node_modules/@arco-design')) return 'arco';
            if (id.includes('node_modules/react') || id.includes('node_modules/scheduler')) return 'react';
            return undefined;
          },
        },
      },
    },
    test: {
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      css: false,
      testTimeout: 20000,
      hookTimeout: 20000,
      restoreMocks: true,
      include: ['src/**/*.test.{ts,tsx}'],
    },
  };
});
