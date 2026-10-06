// Copy of the API reference page (api-docs.html), in both languages. The reference's own copy - the
// home page, the tags, the example titles - lives in docs/contracts/openapi.yaml (description and
// x-description-zh, summary and x-summary-zh); these are the few lines the page adds to it.
export const apiDocsText = {
  zh: {
    language: '中文',
    server: '本服务',
    contentType: '每个写请求都要带，没有请求体也要带（见「约定 › 写请求」）。',
    loadFailed: '接口文档加载失败',
  },
  en: {
    language: 'English',
    server: 'this service',
    contentType: 'Every write sends it, even without a body (see Conventions › Writes).',
    loadFailed: 'Cannot load the API reference',
  },
} as const;
