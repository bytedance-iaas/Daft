/// <reference types="vitest/config" />
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { bundle, createConfig } from '@redocly/openapi-core';
import react from '@vitejs/plugin-react';
import { defineConfig, loadEnv, type Plugin } from 'vite';
import { publicContract } from './src/lib/publicText';

// The mock service worker ships with msw; we serve it in dev and emit it only into demo builds,
// so production bundles never contain mocks.
const MSW_WORKER = fileURLToPath(new URL('./node_modules/msw/lib/mockServiceWorker.js', import.meta.url));

// The C4 contract behind the API reference page (api-docs.html) and {base}/openapi.json.
const CONTRACT = fileURLToPath(new URL('../docs/contracts/openapi.yaml', import.meta.url));

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

/**
 * The contract as one self-contained JSON document: its external $refs (cli/*.schema.json,
 * viz-mapping.schema.json) are hoisted into components.schemas, so the page and agents need no
 * other file, and its texts lose the internal references (src/lib/publicText.ts). JSON, not YAML:
 * the Daemon serves .json as a static file with its MIME type, while an unknown extension like
 * .yaml would get index.html (backend/daemon/routes/static.py).
 */
async function contractJson(): Promise<string> {
  const config = await createConfig({});
  const { bundle: doc, problems } = await bundle({ ref: CONTRACT, config, dereference: false });
  const errors = problems.filter((p) => p.severity === 'error');
  if (errors.length) throw new Error(`cannot bundle ${CONTRACT}: ${errors.map((p) => p.message).join('; ')}`);
  return `${JSON.stringify(publicContract(doc.parsed), null, 1)}\n`;
}

/** {base}/openapi.json next to api-docs.html: emitted by the build, served by the dev server. */
function contractPlugin(base: string): Plugin {
  return {
    name: 'curator-contract',
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        if ((req.url ?? '').split('?')[0] !== `${base}/openapi.json`) return next();
        contractJson().then((text) => {
          res.setHeader('Content-Type', 'application/json; charset=utf-8');
          res.end(text);
        }, next);
      });
    },
    async generateBundle() {
      this.emitFile({ type: 'asset', fileName: 'openapi.json', source: await contractJson() });
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
    plugins: [react(), curatorDevPlugin(base, mode === 'demo', isDev), contractPlugin(base)],
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
        // Two pages: the console, and the API reference (Scalar, a Vue app of its own) that only
        // loads when someone opens it, so it never weighs on the console or meets Arco's styles.
        input: {
          index: fileURLToPath(new URL('./index.html', import.meta.url)),
          'api-docs': fileURLToPath(new URL('./api-docs.html', import.meta.url)),
        },
        output: {
          // Match whole package names: a loose «node_modules/react» also caught react-transition-group,
          // which pulled Arco's helpers into the react chunk and made the two chunks import each
          // other (React was undefined when Arco evaluated).
          manualChunks(id) {
            // Rollup's CommonJS helpers ride with the first CommonJS package that needs them (react);
            // on their own, the API reference page (CommonJS bits too, no React) does not load React.
            if (id.startsWith('\0commonjsHelpers')) return 'commonjs';
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
