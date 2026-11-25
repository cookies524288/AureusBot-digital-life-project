import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'

/** 工作台样式注入 Shadow DOM 后，:root 不再匹配任何元素；同时生成 :host 版本。 */
function expandShadowRootSelectors(css: string): string {
  return css.replace(
    /:root((?:\.[\w-]+|\[[^\]]*\]|:(?!root\b)[\w-]+(?:\([^)]*\))?)*)(?=[\s,{])/g,
    (match, compound: string) =>
      compound ? `:root${compound}, :host(${compound})` : ':root, :host',
  )
}

export default defineConfig({
  base: '/learning-workbench/',
  plugins: [{
    name: 'aureus-shadow-dom-selectors',
    enforce: 'pre',
    transform(source, id) {
      if (id.endsWith('.css')) return expandShadowRootSelectors(source)
    },
  }, tailwindcss(), react(), {
    name: 'aureus-local-fonts',
    enforce: 'pre',
    transform(source, id) {
      if (id.endsWith('.css')) return source.replaceAll("url('/fonts/", "url('/learning-workbench/fonts/")
    },
  }, {
    name: 'aureus-html-line-endings',
    transformIndexHtml: { order: 'post', handler: html => html.replace(/\r+\n/g, '\n') },
  }],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  build: {
    outDir: '../../aureus/web/static/learning-workbench',
    emptyOutDir: true,
    target: 'es2022',
    rollupOptions: {
      output: {
        // 主页面按固定地址动态加载，文件名不能带内容摘要。
        entryFileNames: 'workbench.js',
        assetFileNames: assetInfo => {
          const names = assetInfo.names ?? []
          return names.some(name => name.endsWith('.css'))
            ? 'workbench.css'
            : 'assets/[name]-[hash][extname]'
        },
      },
    },
  },
})
