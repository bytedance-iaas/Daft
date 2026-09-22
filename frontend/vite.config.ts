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

/**
 * What the Daemon puts at <!-- curator:base -->: a static base element (so even the browser's
 * preload scanner resolves ./assets/* under the prefix) and the prefix for the app itself.
 * `base` is validated to URL path characters, so it is safe in both places.
 */
function baseTags(base: string): string {
  return `<base href="${base}/"><script>window.__CURATOR_BASE__ = ${JSON.stringify(base)};</script>`;
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
      return html.replace('<!-- curator:base -->', baseTags(base));
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
          // Match whole package names: a loose «node_modules/react» also caught react-transition-group,
          // which pulled Arco's helpers into the react chunk and made the two chunks import each
          // other (React was undefined when Arco evaluated).
          manualChunks(id) {
            const pkg = /node_modules\/((?:@[^/]+\/)?[^/]+)\//.exec(id.replace(/\\/g, '/'))?.[1];
            if (!pkg) return undefined;
            if (pkg === 'echarts' || pkg === 'zrender') return 'echarts';
            if (pkg === 'react' || pkg === 'react-dom' || pkg === 'scheduler') return 'react';
            if (pkg.startsWith('@arco-design/')) return 'arco';
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
