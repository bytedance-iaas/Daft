// Entry of api-docs.html (vite.config.ts, second input): mounts Scalar. A page of its own, so the
// console never loads Vue or Scalar and Scalar never meets Arco's global styles.
import { createApiReference } from '@scalar/api-reference';
import '@scalar/api-reference/style.css';
import { apiDocsConfiguration, localizeServers, pageLanguage, requireJsonOnBodylessWrites } from './lib/apiDocs';
import { apiDocsText } from './locales/apiDocs';

const root = document.getElementById('api-docs')!;
const lang = pageLanguage(window.location);
document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en';

fetch('./openapi.json')
  .then((res) => {
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json() as Promise<object>;
  })
  .then((doc) => {
    const page = requireJsonOnBodylessWrites(localizeServers(doc, window.location));
    createApiReference(root, apiDocsConfiguration(page, lang));
  })
  .catch((err: unknown) => {
    const why = err instanceof Error ? err.message : String(err);
    root.textContent = `${apiDocsText.zh.loadFailed} / ${apiDocsText.en.loadFailed} (${why})`;
  });
