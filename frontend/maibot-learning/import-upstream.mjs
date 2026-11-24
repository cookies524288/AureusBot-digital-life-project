import { createHash } from 'node:crypto'
import { copyFileSync, existsSync, mkdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { dirname, isAbsolute, join, normalize, relative, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const revision = '062d45cf8bd4272ac57d7ec03fcf405e5a458211'
const target = dirname(fileURLToPath(import.meta.url))
const upstream = resolve(process.argv[2] || '.')
const source = join(upstream, 'src')
const pending = [
  'routes/resource/learning/index.tsx',
  'components/ui/toaster.tsx',
  'components/ui/announcer.tsx',
  'components/ui/millennium-icon-set.ts',
  'index.css',
  ...['controls', 'millennium', 'mobile', 'surfaces', 'data-browser', 'workspace', 'management']
    .map(name => `styles/${name}.css`),
  'i18n/locales/zh.json',
]
const files = new Set(['LICENSE', 'package-lock.json', 'package.json'])
const external = new Set()
const visited = new Set()
const asPosix = path => path.replaceAll('\\', '/')

while (pending.length) {
  const name = asPosix(normalize(pending.shift()))
  if (visited.has(name)) continue
  visited.add(name)
  const text = readFileSync(join(source, name), 'utf8')
  files.add(`src/${name}`)
  if (name.endsWith('.css')) {
    for (const [match, asset] of text.matchAll(/url\(['"]?(\/fonts\/[^'"\s)]+)/g)) {
      const font = `public${asset}`
      if (!existsSync(join(upstream, font))) throw new Error(`缺少字体：${match}`)
      files.add(font)
    }
    continue
  }
  if (!/\.[jt]sx?$/.test(name)) continue
  for (const [, specifier] of text.matchAll(/(?:\bfrom\s*|\bimport\s*(?:\(\s*)?)['"]([^'"]+)['"]/g)) {
    if (!specifier.startsWith('@/') && !specifier.startsWith('.')) {
      external.add(specifier)
      continue
    }
    const path = specifier.startsWith('@/') ? specifier.slice(2) : join(dirname(name), specifier)
    const resolved = ['', '.ts', '.tsx', '.d.ts', '.js', '/index.ts', '/index.tsx'].map(suffix => path + suffix)
      .find(candidate => existsSync(join(source, candidate)) && statSync(join(source, candidate)).isFile())
    if (!resolved) throw new Error(`${name}: 无法解析 ${specifier}`)
    pending.push(resolved)
  }
}

for (const name of ['Jersey20-OFL.txt', 'DepartureMono-OFL.txt', 'ArkPixel12-OFL.txt', 'JetBrainsMono-OFL.txt']) {
  if (existsSync(join(upstream, 'public/fonts', name))) files.add(`public/fonts/${name}`)
}

const manifest = {}
for (const name of [...files].sort()) {
  const input = join(upstream, name)
  const output = resolve(target, name === 'package.json' ? 'package.json' : name)
  const location = relative(target, output)
  if (location.startsWith('..') || isAbsolute(location)) throw new Error('导入路径越界')
  mkdirSync(dirname(output), { recursive: true })
  copyFileSync(input, output)
  manifest[asPosix(location)] = {
    source: `dashboard/${name}`,
    sha256: createHash('sha256').update(readFileSync(input)).digest('hex'),
  }
}

writeFileSync(join(target, 'upstream-files.json'), `${JSON.stringify({ revision, files: manifest }, null, 2)}\n`)
console.log(`Vendor: ${files.size} files. External: ${[...external].sort().join(', ')}`)
