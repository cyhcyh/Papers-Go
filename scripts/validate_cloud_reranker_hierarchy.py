"""Experimental taxonomy beam routing and one cloud decision; no application writes."""
import argparse
import asyncio
from collections import defaultdict
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import time
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
sys.stdout.reconfigure(encoding='utf-8')

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field
from app.llm.secrets import decrypt_configuration
from app.llm.provider import completion_options
from app.llm.thinking import clean_answer


class Choice(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    choice: int = Field(ge=0)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=300)
    evidence: str = Field(max_length=400)


INSTRUCTION = (
    'Determine whether the candidate scientific classification includes the primary research problem '
    'and main contribution of the paper. A broad category is relevant if it contains the primary '
    'research subfield. Focus on the main contribution rather than incidental methods, applications, '
    'symbols or mentioned concepts. Use the complete classification path to distinguish similarly '
    'named concepts in different fields. Rank candidate classifications by suitability for the primary subject.'
)

PROMPT = '''根据论文标题和摘要，选择主要研究问题或核心贡献最相关的一个标准分类。
先确定主要贡献，再核对候选的完整路径与适用范围。共享词语不等于同一主题，使用工具或应用到某领域不代表研究该工具或该领域。理论、方法、软件、硬件等限定必须与主要贡献相符。
候选包含不同粒度：只在摘要支持时选具体条目，证据不足则选择适用的较宽条目。不能从候选之外编造名称或编码，也不必强行选择最细条目。
choice 必须是候选编号；如果所有候选均不适用，choice=0，并在 reason 中说明主要方向和缺少的主题。只选择一个主主题，不输出次主题。
reason 用不超过60字的中文说明对应关系；evidence 引用标题或摘要中8～120字符连续原文，不改写、不加省略号。
只输出一个 JSON，不输出思考或 Markdown：
{"choice":1,"confidence":0.8,"reason":"简短中文依据","evidence":"连续原文"}'''


def usable(entry):
    return bool(entry['parent']) and (entry['system'] != 'MSC2020' or
        '-' not in entry['code'] and not entry['code'].endswith('99'))


async def main(args):
    with sqlite3.connect((ROOT / 'data/papers.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        config = decrypt_configuration(json.loads(db.execute(
            "SELECT value FROM app_settings WHERE name='models'").fetchone()[0]))
    selection = config['routes']['classify']['primary']
    connection = next(c for c in config['connections'] if c['id'] == selection['connection_id'])
    origin = urlsplit(connection['base_url'])
    if origin.scheme != 'https' or not (origin.hostname or '').endswith('.cn-beijing.maas.aliyuncs.com'):
        raise ValueError('Use the existing Beijing Model Studio provider for this experiment.')
    endpoint = urlunsplit((origin.scheme, origin.netloc, '/compatible-api/v1/reranks', '', ''))
    binding = {**connection, **selection, 'model': 'deepseek-v4.1-flash', 'thinking': 'off',
               'reasoning_effort': 'auto', 'feature': 'classify'}
    request_options = completion_options(binding, binding['model'], binding['base_url'])
    entries = json.loads((ROOT / 'backend/app/resources/standard_catalog.json').read_text(encoding='utf-8'))
    by_key = {e['key']: e for e in entries}
    children = defaultdict(list)
    for entry in entries:
        children[entry['parent']].append(entry)

    def path(entry):
        labels = []
        while entry:
            labels.append(entry['label'])
            entry = by_key.get(entry['parent'])
        return ' › '.join(reversed(labels))

    def document(entry):
        text = entry['code'] + ' ' + path(entry)
        subs = [e['label'] for e in children[entry['key']] if usable(e)]
        if subs:
            text += '\nImmediate subfields: ' + '; '.join(subs[:12])
        return text

    papers = json.loads(Path(args.sample).read_text(encoding='utf-8'))['results']
    if args.ids:
        ids = {int(i) for i in args.ids.split(',')}
        papers = [p for p in papers if p['id'] in ids]
    results = []
    semaphore = asyncio.Semaphore(args.concurrency)
    started = time.perf_counter()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.with_suffix('.prompt.txt').write_text(PROMPT, encoding='utf-8')
    output.with_suffix('.jsonl').write_text('', encoding='utf-8')
    async with httpx.AsyncClient(timeout=45, follow_redirects=False) as reranker, AsyncOpenAI(
            base_url=binding['base_url'], api_key=binding['api_key'], timeout=45, max_retries=0) as llm:
        async def process(paper):
            async with semaphore:
                row = {k: paper.get(k) for k in ('id', 'title', 'abstract', 'category', 'venue')}
                row['system'] = paper.get('system') or ('MSC2020' if (paper.get('category') or '').startswith('math.') else 'CCS2012')
                row['rerank_requests'] = []
                row['stages'] = []
                before = time.perf_counter()
                query = row['title'] + '\n' + (row.get('abstract') or '')

                async def rank(options, stage):
                    # Batch scores are only comparable within one request. When a
                    # pool needs splitting, rerank the shortlists together once.
                    parts = [options[i:i + 96] for i in range(0, len(options), 96)]
                    shortlisted = []
                    for batch in parts:
                        call_start = time.perf_counter()
                        response = await reranker.post(endpoint, headers={'Authorization': 'Bearer ' + binding['api_key']},
                            json={'model': 'qwen3-rerank', 'query': query, 'documents': [document(e) for e in batch],
                                  'top_n': len(batch) if len(parts) == 1 else min(16, len(batch)), 'instruct': INSTRUCTION})
                        response.raise_for_status()
                        payload = response.json()
                        scored = payload.get('results') or payload.get('output', {}).get('results')
                        if not scored:
                            raise ValueError('No reranking results.')
                        row['rerank_requests'].append({'stage': stage, 'documents': len(batch),
                            'seconds': round(time.perf_counter() - call_start, 3), 'usage': payload.get('usage', {})})
                        shortlisted.extend({**batch[item['index']], 'score': item['relevance_score']} for item in scored)
                    if len(parts) > 1:
                        return await rank(shortlisted, stage + '_merge')
                    return shortlisted

                def beam(ranked, parents, width):
                    # Keep each surviving branch represented before filling the
                    # remaining slots by rank; never merge scores across calls.
                    chosen = []
                    seen = set()
                    for parent in parents:
                        match = next((e for e in ranked if e['parent'] == parent['key'] or e['key'] == parent['key']), None)
                        if match and match['key'] not in seen:
                            chosen.append(match); seen.add(match['key'])
                    for entry in ranked:
                        if len(chosen) >= width:
                            break
                        if entry['key'] not in seen:
                            chosen.append(entry); seen.add(entry['key'])
                    return chosen[:width]

                try:
                    roots = [e for e in children[None] if e['system'] == row['system']]
                    root_ranked = await rank(roots, 'roots')
                    frontier = root_ranked[:args.root_beam]
                    row['root_top'] = [{'key': e['key'], 'label': e['label'], 'score': e['score']} for e in frontier]
                    max_depth = 3 if row['system'] == 'MSC2020' else 5
                    last_ranked = frontier
                    last_parents = []
                    for depth in range(2, max_depth + 1):
                        options = {}
                        for parent in frontier:
                            subs = [e for e in children[parent['key']] if usable(e)]
                            for entry in subs or ([parent] if usable(parent) else []):
                                options[entry['key']] = entry
                        if not options:
                            break
                        ranked = await rank(list(options.values()), 'depth_' + str(depth))
                        width = args.branch_beam if depth == 2 else args.fine_beam
                        last_parents = frontier
                        frontier = beam(ranked, frontier, width)
                        last_ranked = ranked
                        row['stages'].append({'depth': depth, 'considered': len(options),
                            'selected': [{'key': e['key'], 'label': e['label'], 'score': e['score']} for e in frontier]})
                        if all(not any(usable(c) for c in children[e['key']]) for e in frontier):
                            break
                    # Specific candidates and broader alternatives receive slots
                    # separately, preventing broad parent scores dominating leaves.
                    fine = beam(last_ranked, last_parents, args.fine_beam)
                    finalists = {e['key']: e for e in fine}
                    for entry in last_ranked[:args.fine_beam]:
                        parent = by_key.get(entry['parent'])
                        if parent and usable(parent):
                            finalists.setdefault(parent['key'], parent)
                    broad = [e for e in finalists.values() if e['key'] not in {x['key'] for x in fine}]
                    if broad:
                        broad = await rank(broad, 'broader_alternatives')
                    options = fine + broad[:args.parent_slots]
                    row['candidates'] = [{'key': e['key'], 'code': e['code'], 'label': e['label'], 'path': path(e)} for e in options]
                    row['candidate_count'] = len(options)
                    row['coarse_seconds'] = round(time.perf_counter() - before, 3)
                    material = {'system': row['system'], 'title': row['title'], 'abstract': row.get('abstract') or '',
                                'candidates': [{'id': i + 1, 'path': e['path']} for i, e in enumerate(row['candidates'])]}
                    cloud_start = time.perf_counter()
                    response = await llm.chat.completions.create(model=binding['model'], temperature=.3,
                        response_format={'type': 'json_object'}, messages=[{'role': 'system', 'content': PROMPT},
                        {'role': 'user', 'content': json.dumps(material, ensure_ascii=False)}], **request_options)
                    row['cloud_seconds'] = round(time.perf_counter() - cloud_start, 3)
                    row['usage'] = {'input_tokens': response.usage.prompt_tokens, 'output_tokens': response.usage.completion_tokens,
                                    'total_tokens': response.usage.total_tokens}
                    row['reasoning_observed'] = bool(getattr(response.choices[0].message, 'reasoning_content', None))
                    row['finish_reason'] = response.choices[0].finish_reason
                    if row['finish_reason'] != 'stop':
                        raise ValueError('Incomplete final response.')
                    decision = Choice.model_validate_json(clean_answer(response.choices[0].message.content or '')).model_dump()
                    if decision['choice'] > len(options):
                        raise ValueError('Final choice outside candidate list.')
                    row['decision'] = decision
                    selected = row['candidates'][decision['choice'] - 1] if decision['choice'] else None
                    row.update(ok=True, final_key=selected['key'] if selected else None,
                               final_label=selected['label'] if selected else None,
                               final_path=selected['path'] if selected else None)
                    evidence = ' '.join(decision['evidence'].casefold().split())
                    row['evidence_exact'] = len(evidence) >= 8 and evidence in ' '.join(query.casefold().split())
                except Exception as error:
                    row.update(ok=False, error_type=type(error).__name__,
                        http_status=getattr(getattr(error, 'response', None), 'status_code', None))
                row['seconds'] = round(time.perf_counter() - before, 3)
                results.append(row)
                with output.with_suffix('.jsonl').open('a', encoding='utf-8') as file:
                    file.write(json.dumps(row, ensure_ascii=False) + '\n')
                if len(results) % 4 == 0 or len(results) == len(papers):
                    print(json.dumps({'completed': len(results), 'total': len(papers),
                        'failed': sum(not r['ok'] for r in results),
                        'elapsed_seconds': round(time.perf_counter() - started, 2)}), flush=True)

        await asyncio.gather(*(process(p) for p in papers))
    good = [r for r in results if r['ok']]
    summary = {'reranker_model': 'qwen3-rerank', 'reranker_size': 'Provider does not specify open-weight size',
        'final_model': binding['model'], 'thinking': 'off', 'concurrency': args.concurrency,
        'root_beam': args.root_beam, 'branch_beam': args.branch_beam,
        'fine_beam': args.fine_beam, 'parent_slots': args.parent_slots,
        'papers': len(results), 'failed': len(results) - len(good), 'assigned': sum(bool(r.get('final_key')) for r in results),
        'unmatched': sum(r['ok'] and not r.get('final_key') for r in results),
        'rerank_calls': sum(len(r['rerank_requests']) for r in results),
        'rerank_document_pairs': sum(c['documents'] for r in results for c in r['rerank_requests']),
        'rerank_total_tokens': sum(c['usage'].get('total_tokens', 0) for r in results for c in r['rerank_requests']),
        'deepseek_calls': sum('usage' in r for r in results),
        'deepseek_total_tokens': sum(r.get('usage', {}).get('total_tokens', 0) for r in results),
        'average_deepseek_tokens': round(statistics.mean(r['usage']['total_tokens'] for r in good), 1) if good else None,
        'average_coarse_seconds': round(statistics.mean(r['coarse_seconds'] for r in good), 3) if good else None,
        'average_cloud_seconds': round(statistics.mean(r['cloud_seconds'] for r in good), 3) if good else None,
        'average_seconds': round(statistics.mean(r['seconds'] for r in good), 3) if good else None,
        'average_candidates': round(statistics.mean(r['candidate_count'] for r in good), 1) if good else None,
        'batch_seconds': round(time.perf_counter() - started, 3),
        'reasoning_observed_calls': sum(r.get('reasoning_observed', False) for r in results)}
    output.write_text(json.dumps({'scope': 'Experimental only; no application labels, tables or model configuration changed.',
        'instruction': INSTRUCTION, 'decision_prompt': PROMPT, 'summary': summary,
        'results': sorted(results, key=lambda r: r['id'])}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sample', default=str(ROOT / 'artifacts/classification-gliclass-coarse-results-20261003.json'))
    parser.add_argument('--output', required=True)
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--root-beam', type=int, default=3)
    parser.add_argument('--branch-beam', type=int, default=5)
    parser.add_argument('--fine-beam', type=int, default=8)
    parser.add_argument('--parent-slots', type=int, default=2)
    parser.add_argument('--ids')
    asyncio.run(main(parser.parse_args()))
