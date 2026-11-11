// Lint rules for the UI. Run: npm run lint
// CI runs exactly this. Errors are mistakes; style is left to the code's own conventions.
import vue from 'eslint-plugin-vue'
import tseslint from 'typescript-eslint'
import vueParser from 'vue-eslint-parser'

export default tseslint.config(
  { ignores: ['dist/**', 'node_modules/**', 'test-results/**', 'playwright-report/**', 'coverage/**', 'auto-imports.d.ts', 'components.d.ts'] },
  ...tseslint.configs.recommended,
  ...vue.configs['flat/essential'],
  {
    files: ['**/*.vue'],
    languageOptions: {
      parser: vueParser,
      parserOptions: { parser: tseslint.parser, ecmaVersion: 'latest', sourceType: 'module', extraFileExtensions: ['.vue'] },
    },
  },
  {
    rules: {
      // Element Plus components and the API helpers are auto-imported (unplugin-auto-import), so the
      // TypeScript check, which knows them, decides what is undefined; ESLint cannot.
      'no-undef': 'off',
      // `any` is reported, not refused: about 200 places still use it, mostly API responses. The number is
      // printed by `npm run lint`; new code should bring it down, and the rule becomes an error at zero.
      '@typescript-eslint/no-explicit-any': 'warn',
      '@typescript-eslint/no-unused-vars': ['error', { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrors: 'none' }],
      // Page components are named for what they are (Report, Settings, Layout).
      'vue/multi-word-component-names': 'off',
      // The editor's sub-forms edit fields of the object they are given (v-model="config.x"): one shared
      // reactive plan tree, by design. Replacing the prop itself is still an error.
      'vue/no-mutating-props': ['error', { shallowOnly: true }],
    },
  },
)
