// Entry of api-docs.html (vite.config.ts, second input): mounts Scalar. A page of its own, so the
// console never loads Vue or Scalar and Scalar never meets Arco's global styles.
import { createApiReference } from '@scalar/api-reference';
import '@scalar/api-reference/style.css';
import { apiDocsConfiguration, localizeServers, requireJsonOnBodylessWrites } from './lib/apiDocs';

const root = document.getElementById('api-docs')!;

fetch('./openapi.json')
  .then((res) => {
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json() as Promise<object>;
  })
  .then((doc) => {
    const page = requireJsonOnBodylessWrites(localizeServers(doc, window.location));
    createApiReference(root, apiDocsConfiguration(page));
  })
  .catch((err: unknown) => {
    root.textContent = `Cannot load openapi.json (${err instanceof Error ? err.message : String(err)})`;
  });
