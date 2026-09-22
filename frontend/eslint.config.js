import js from '@eslint/js';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist', 'node_modules', 'src/api/schema.d.ts', 'coverage'] },
  {
    files: ['**/*.{ts,tsx}'],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2022,
      globals: { ...globals.browser },
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      'react-refresh/only-export-components': 'off',
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_' }],
      '@typescript-eslint/consistent-type-imports': 'error',
      'no-restricted-syntax': [
        'error',
        {
          // Every URL must be built from the mount prefix (doc 07 §2.3, §9): no hard-coded API roots.
          selector: "Literal[value=/^\\/(api|events)\\//]",
          message: 'Build API and SSE URLs from src/base.ts; never hard-code the root path.',
        },
        // All UI text lives in src/locales/zh.ts (doc 07 §9), so it can be reviewed in one place.
        { selector: 'Literal[value=/[\\u4e00-\\u9fff]/]', message: 'Put UI text in src/locales/zh.ts.' },
        { selector: 'TemplateElement[value.raw=/[\\u4e00-\\u9fff]/]', message: 'Put UI text in src/locales/zh.ts.' },
        { selector: 'JSXText[value=/[\\u4e00-\\u9fff]/]', message: 'Put UI text in src/locales/zh.ts.' },
      ],
    },
  },
  {
    // The text itself.
    files: ['src/locales/**'],
    rules: { 'no-restricted-syntax': 'off' },
  },
  {
    files: ['src/**/*.test.{ts,tsx}', 'src/test/**', 'src/mocks/**'],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
    rules: { 'no-restricted-syntax': 'off' },
  },
  {
    files: ['scripts/**/*.mjs', 'eslint.config.js'],
    extends: [js.configs.recommended],
    languageOptions: { globals: { ...globals.node } },
  },
);
