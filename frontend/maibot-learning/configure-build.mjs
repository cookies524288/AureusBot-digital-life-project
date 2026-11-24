import { readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(fileURLToPath(import.meta.url))
const upstream = JSON.parse(readFileSync(join(root, 'upstream-package-lock.json'), 'utf8'))
const packages = new Set(['i18next'])
const visit = directory => {
  for (const item of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, item.name)
    if (item.isDirectory()) visit(path)
    else if (/\.[jt]sx?$/.test(item.name)) {
      const text = readFileSync(path, 'utf8')
      for (const [, specifier] of text.matchAll(/(?:\bfrom\s*|\bimport\s*)['"]([^'"]+)['"]/g)) {
        if (specifier.startsWith('.') || specifier.startsWith('@/')) continue
        const parts = specifier.split('/')
        packages.add(specifier.startsWith('@') ? parts.slice(0, 2).join('/') : parts[0])
      }
    }
  }
}
visit(join(root, 'src'))
const exact = name => {
  const item = upstream.packages[`node_modules/${name}`]
  if (!item) throw new Error(`上游锁文件缺少 ${name}`)
  return item.version
}
const dev = ['@types/node', '@types/react', '@types/react-dom', '@vitejs/plugin-react', '@tailwindcss/vite', 'tailwindcss', 'typescript', 'vite']
const manifest = {
  name: 'aureus-learning-workbench', version: '1.9.9', private: true, type: 'module',
  license: 'AGPL-3.0-only',
  scripts: { build: 'tsc --noEmit && vite build', dev: 'vite --host 127.0.0.1', preview: 'vite preview --host 127.0.0.1' },
  dependencies: Object.fromEntries([...packages].sort().map(name => [name, exact(name)])),
  devDependencies: Object.fromEntries(dev.map(name => [name, exact(name)])),
}
writeFileSync(join(root, 'package.json'), `${JSON.stringify(manifest, null, 2)}\n`)
const lock = { ...upstream, name: manifest.name, version: manifest.version, packages: { ...upstream.packages, '': manifest } }
writeFileSync(join(root, 'package-lock.json'), `${JSON.stringify(lock, null, 2)}\n`)
