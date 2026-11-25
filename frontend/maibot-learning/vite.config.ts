import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { defineConfig } from 'vite'

export default defineConfig({
  base: '/learning-workbench/',
  plugins: [tailwindcss(), react(), {
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
  },
})
