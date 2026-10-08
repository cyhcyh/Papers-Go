import assert from 'node:assert/strict'
import {mkdir} from 'node:fs/promises'
import {fileURLToPath, pathToFileURL} from 'node:url'
import {build} from 'esbuild'
import {createElement} from 'react'
import {renderToStaticMarkup} from 'react-dom/server'

const output = new URL('../node_modules/.cache/math-check/MathText.mjs', import.meta.url)
await mkdir(new URL('.', output), {recursive: true})
await build({entryPoints: [fileURLToPath(new URL('../src/components/MathText.tsx', import.meta.url))], outfile: fileURLToPath(output), bundle: true, platform: 'node', format: 'esm', packages: 'external', jsx: 'automatic'})
const {MathText, splitMath} = await import(pathToFileURL(fileURLToPath(output)).href)
const render = (text, inline = false) => renderToStaticMarkup(createElement(MathText, {inline}, text))

const formulas = String.raw`复杂度 $O(2^t)$，概率 \(p=\frac{1}{2}\)。$$\sum_{i=1}^{n} i$$ 和 \[x^2+y^2=z^2\]`
assert.equal(splitMath(formulas).filter(p => p.math).length, 4)
assert.equal(splitMath(formulas).filter(p => p.display).length, 2)
assert.equal((render(formulas).match(/class="katex-mathml"/g) || []).length, 4)
assert.match(render(formulas), /<mfrac>/)
assert.match(render(formulas), /<munderover>/)
assert.equal((render(formulas, true).match(/class="math-display"/g) || []).length, 0)

for (const text of [String.raw`美元 \$5 与 \$10`, '$5 and $10', '`$x$`', '```latex\n$x$\n```', '$x', '\\(x', '```latex\n$x$']) {
  assert.equal(splitMath(text).filter(p => p.math).length, 0, text)
  assert.doesNotThrow(() => render(text))
}
assert.match(render('$x^2$'), /katex-mathml/)
assert.match(render(String.raw`$\frac{x}{$`), /katex-error/)
assert.match(render('<img src=x onerror=alert(1)> $x$'), /&lt;img/)
assert.doesNotMatch(render(String.raw`$\href{javascript:alert(1)}{click}$`), /href="javascript:/)
assert.match(render('第一行\n$x$\n第三行'), /第一行\n/)
assert.equal(splitMath('plain text')[0].text, 'plain text')
console.log('Math rendering checks passed: delimiters, streaming, code, invalid formulas, and escaped HTML.')
