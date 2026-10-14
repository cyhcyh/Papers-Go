import assert from 'node:assert/strict'
import {mkdir} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
import {spawnSync} from 'node:child_process'
import {build} from 'esbuild'

const output=new URL('../.checks/trends/TrendContent.mjs',import.meta.url)
await mkdir(new URL('.',output),{recursive:true})
await build({stdin:{contents:"export {TrendContent} from './TrendContent';export {ChatContent} from './ChatContent'",resolveDir:fileURLToPath(new URL('../src/components',import.meta.url)),loader:'ts'},outfile:fileURLToPath(output),bundle:true,platform:'node',format:'esm',packages:'external',jsx:'automatic',loader:{'.css':'empty'}})
const analysis='本期研究有两条主线。\n\n1. **结构条件与边界**：给定条件下研究 $R(s,t)$ 的下界。\n2. **构造方法**：比较方法的适用范围。\n\n- 结论适用于所研究的参数。'
const formulas=String.raw`本期概述包含 $n^2+2$。

1. **主要进展**。研究给出 $\Theta(n\sqrt{\log n})$，参见[增长阶研究](paper:7)。
2. **近期对照**.结果满足 $$\sum_{i=1}^{n} i=\frac{n(n+1)}2$$。`
const check=`
 import assert from 'node:assert/strict';
 import {createElement} from 'react';
 import {renderToStaticMarkup} from 'react-dom/server';
 import {TrendContent,ChatContent} from ${JSON.stringify(output.href)};
 const render=(text,compact=false)=>renderToStaticMarkup(createElement(TrendContent,{text,compact}));
 const html=render(${JSON.stringify(analysis)});
 assert.match(html,/<ol>/);assert.match(html,/<ul>/);
 assert.equal((html.match(/<li>/g)||[]).length,3);
 assert.match(html,/<strong>.*?结构条件与边界.*?<\\/strong>/);
 assert.match(html,/katex-mathml/);assert.doesNotMatch(html,/<p[^>]*><div/);
 const short=render('近期研究主要关注**结构条件**与**构造方法**。',true);
 assert.match(short,/trend-markdown-compact/);assert.equal((short.match(/<strong>/g)||[]).length,2);
 assert.doesNotMatch(short,/\\*\\*/);
 assert.match(render('旧缓存的普通中文正文。'),/旧缓存的普通中文正文/);
 assert.match(render('<img src=x onerror=alert(1)>'),/&lt;img/);
 const full=(text,references=[],onOpenPaper=()=>{})=>renderToStaticMarkup(createElement(TrendContent,{text,analysis:true,references,onOpenPaper}));
 for(const punctuation of ['。','.',':','：']){
  const body=full('1. **标题**'+punctuation+'正文中的句号。仍然保留。').match(/class="trend-progress-body">([\\s\\S]*?)<\\/div>/)[1];
  assert.equal(body.replace(/<[^>]*>/g,''),'正文中的句号。仍然保留。');
 }
 const references=[{id:7,title:'Full Original Paper Title',url:'https://arxiv.org/abs/2610.00007'}];
 const formatted=full(${JSON.stringify(formulas)},references);
 assert.equal((formatted.match(/class="katex-mathml"/g)||[]).length,3);
 assert.match(formatted,/math-display/);assert.match(formatted,/<mfrac>/);
 assert.match(formatted,/<span role="button" tabindex="0" aria-haspopup="dialog" aria-label="Full Original Paper Title" class="trend-paper-reference" title="Full Original Paper Title"/);
 assert.doesNotMatch(formatted,/paper:7|href=|target="_blank"/);
 const unresolved=full('1. **进展**。[论文简称](paper:7)。');
 assert.match(unresolved,/论文简称/);assert.doesNotMatch(unresolved,/<a |role="button"|paper:7/);
 const withoutHandler=renderToStaticMarkup(createElement(TrendContent,{text:'[论文简称](paper:7)',analysis:true,references}));
 assert.doesNotMatch(withoutHandler,/<a |role="button"|paper:7/);
 assert.match(full('[论文简称](paper:7)',[{id:7,title:'No URL required'}]),/trend-paper-reference/);
 const unsafeUrl=full('[论文简称](paper:7)',[{id:7,title:'Unsafe source',url:'javascript:alert(1)'}]);
 assert.match(unsafeUrl,/trend-paper-reference/);assert.doesNotMatch(unsafeUrl,/href=|javascript:/);
 const boldReference=full('1. **进展**\\n\\n**[Original Paper](paper:7)** 与 [**Original Paper**](paper:7)。',references);
 const boldMarkup=boldReference.replace(/<span class="math-text">([^<]*)<\\/span>/g,'$1');
 assert.match(boldMarkup,/<strong><span[^>]*>Original Paper<\\/span><\\/strong>/);
 assert.match(boldMarkup,/<span[^>]*><strong>Original Paper<\\/strong><\\/span>/);
 assert.doesNotMatch(boldReference,/paper:7|href=|\\*\\*/);
 const longTitle='Edge and spectral conditions for rainbow bipancyclicity in bipartite graph collections';
 const shortened=full('1. **进展**\\n\\n['+longTitle+'](paper:8)。',[{id:8,title:longTitle}]);
 const visible=shortened.match(/class="trend-paper-reference"[^>]*><span class="math-text">([^<]+)<\\/span>/)[1];
 assert.ok(visible.endsWith('…')&&Array.from(visible).length<=48);
 assert.ok(longTitle.startsWith(visible.slice(0,-1))&&longTitle[visible.length-1]===' ');
 assert.match(shortened,/title="Edge and spectral conditions for rainbow bipancyclicity in bipartite graph collections"/);
 assert.match(shortened,/aria-label="Edge and spectral conditions for rainbow bipancyclicity in bipartite graph collections"/);
 assert.doesNotMatch(shortened,/<button|href=/);
 const shortLabel=full('[Original Paper](paper:8)',[{id:8,title:longTitle}]);
 assert.match(shortLabel,/>Original Paper<\\/span>/);assert.doesNotMatch(shortLabel,/…/);
 const unicodeTitle='原语言标题与研究方向'.repeat(8)+'🧮';
 const unicode=full('['+unicodeTitle+'](paper:9)',[{id:9,title:unicodeTitle}]);
 assert.match(unicode,/…/);assert.doesNotMatch(unicode,/\\uFFFD/);
 const mathTitle='A study with $n^2$ '+longTitle;
 const mathLabel=full('['+mathTitle+'](paper:8)',[{id:8,title:mathTitle}]);
 assert.match(mathLabel,/katex-mathml/);assert.doesNotMatch(mathLabel,/\\uE000|\\uE001/);
 const shortenedBold=full('[**'+longTitle+'**](paper:8)',[{id:8,title:longTitle}]);
 assert.match(shortenedBold,/<strong>[\\s\\S]*…[\\s\\S]*<\\/strong>/);assert.doesNotMatch(shortenedBold,/\\*\\*/);
 const selected=[],controls=[];
 const interactive=ChatContent({text:'1. **进展**\\n\\n**[Original Paper](paper:7)**、[**Original Paper**](paper:7)、[公式 $n^2$](paper:7)、[旧引用](https://arxiv.org/abs/2610.00007)、[未知引用](paper:99)。',presentation:'trend-analysis',references,onOpenPaper:id=>selected.push(id)});
 const visit=node=>{
  if(Array.isArray(node)){node.forEach(visit);return;}
  if(!node||typeof node!=='object')return;
  if(typeof node.type==='function'&&node.props.math){visit(node.type(node.props));return;}
  if(node.type==='span'&&node.props.role==='button')controls.push(node);
  visit(node.props?.children);
 };
 visit(interactive);
 assert.deepEqual(selected,[]);assert.equal(controls.length,4);
 for(const control of controls){assert.equal(control.props.tabIndex,0);assert.equal(control.props.href,undefined);control.props.onClick();}
 assert.deepEqual(selected,[7,7,7,7]);
 let prevented=0;
 for(const control of controls){for(const key of ['Enter',' ','Escape'])control.props.onKeyDown({key,preventDefault:()=>prevented++});}
 assert.equal(prevented,8);assert.deepEqual(selected,Array(12).fill(7));
 assert.doesNotMatch(full('[旧引用](https://arxiv.org/abs/2610.00007)',references),/href=/);
 assert.match(full('[其它网站](https://example.org)'),/href="https:\\/\\/example.org"/);
 assert.match(full('\\x60[代码中的引用](paper:7)\\x60',references),/<code>\\[代码中的引用\\]\\(paper:7\\)<\\/code>/);
 assert.match(render('[保持原文](paper:7)'),/\\[保持原文\\]\\(paper:7\\)/);
 const separated='概述。\\n\\n1. **共同问题**\\n\\n第一段证据。\\n\\n第二段解释。\\n\\n1. **另一趋势**\\n下一段解释。';
 const grouped=full(separated);
 assert.equal((grouped.match(/class="trend-summary-label"/g)||[]).length,1);
 assert.equal((grouped.match(/class="trend-progress"/g)||[]).length,2);
 assert.match(grouped,/trend-progress-number">01<\\/span>/);assert.match(grouped,/trend-progress-number">02<\\/span>/);
 const points=grouped.split('class="trend-progress"').slice(1);
 assert.match(points[0],/trend-progress-body">[\\s\\S]*第一段证据。[\\s\\S]*第二段解释。[\\s\\S]*<\\/div><\\/li><\\/ol>/);
 assert.doesNotMatch(points[0],/下一段解释/);assert.match(points[1],/trend-progress-body">[\\s\\S]*下一段解释。[\\s\\S]*<\\/div><\\/li><\\/ol>/);
 const rich=full('1. **证据与比较**\\n\\n| 研究 | 结果 |\\n| --- | --- |\\n| A | $n^2$ |\\n\\n- 适用条件\\n\\n1. 普通编号\\n2. 普通编号二\\n3. **后续趋势**\\n\\n后续正文。');
 assert.equal((rich.match(/class="trend-progress"/g)||[]).length,2);
 assert.match(rich.split('class="trend-progress"')[1],/trend-progress-body">[\\s\\S]*<table>[\\s\\S]*katex-mathml[\\s\\S]*<ul>[\\s\\S]*普通编号二[\\s\\S]*<\\/div><\\/li><\\/ol>/);
 assert.doesNotMatch(render(separated),/trend-progress|trend-summary/);
 assert.match(full('旧缓存的普通中文正文。'),/旧缓存的普通中文正文/);
 console.log('Trend rendering checks passed: bounded original-language labels, inline accessible references, mouse and keyboard paper-open callbacks, full-title tooltips, bold text, grouped bodies, tables, lists, math and unchanged normal Markdown.');
`
const result=spawnSync(process.execPath,['--max-old-space-size=96','--input-type=module','-e',check],{cwd:fileURLToPath(new URL('..',import.meta.url)),timeout:10000,encoding:'utf8',maxBuffer:128*1024})
assert.equal(result.status,0,`Trend renderer failed: ${result.stdout}\n${result.stderr}`)
console.log(result.stdout.trim())
