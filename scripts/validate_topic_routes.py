"""Compare an improved direct prompt and conditional local taxonomy alignment.

The alignment route reuses the identical first-pass responses, only adding a
cloud decision when local alignment fails. Everything stays in temporary DBs
and evaluation files; there is no production label or model-config mutation.
"""
import argparse
import asyncio
from collections import Counter
import json
import math
from pathlib import Path
import re
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pydantic import BaseModel, ConfigDict, Field
from app.config import settings, now
from app.db import init_db, pack
from app.llm import runtime
from app.llm.provider import cloud, completion_options
from app.standard_topics import catalog
from app.topic_semantics import cache, index_entries
from app.topic_retrieval import retrieve, concept


class CoreDecision(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    system: str
    core_topic_en: str = Field(min_length=3, max_length=250)
    code: str | None
    name_en: str | None
    path_en: list[str] = Field(max_length=10)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=200)
    evidence: str = Field(max_length=240)


class AlignedDecision(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    choice: int | None
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=200)
    evidence: str = Field(max_length=240)


def normalized(value):
    return re.sub(r'[^a-z0-9]+', ' ', unicodedata.normalize('NFKC', value or '').casefold()).strip()


def quote_valid(paper, evidence):
    norm = lambda text: ' '.join(unicodedata.normalize('NFKC', text).casefold().split())
    value = norm(evidence)
    return len(value) >= 8 and value in norm(paper['title'] + '\n' + (paper.get('abstract') or ''))


def declared_path_matches(entry, declared):
    if not declared:
        return True
    ancestors = [normalized(v) for v in entry['path'].split(' › ')]
    position = 0
    for label in declared:
        label = normalized(label)
        if not label:
            continue
        found = next((i for i in range(position, len(ancestors))
                      if label == ancestors[i] or len(label) >= 8 and ancestors[i].startswith(label + ' ')), None)
        if found is None:
            return False
        position = found + 1
    return True


def directory_matches(decision, entries):
    code = (decision.get('code') or '').removeprefix(decision['system'] + ':')
    name = normalized(decision.get('name_en'))
    named = [e for e in entries if name and normalized(e['label']) == name]
    coded = [e for e in entries if code and (e['code'] == code or e['system'] == 'CCS2012' and e['code'].split('.')[-1] == code)]
    compatible = [e for e in coded if not name or normalized(e['label']) == name
                  or len(name) >= 8 and normalized(e['label']).startswith(name + ' ')]
    matches = list({e['key']: e for e in compatible + named}.values())
    return matches, {'name_in_catalog': bool(named), 'code_in_catalog': bool(coded),
                     'path_consistent_match': any(declared_path_matches(e, decision['path_en']) for e in matches)}


async def cloud_request(primary, messages, schema):
    binding = {**primary, 'feature': 'classify'}
    with runtime.bind(binding):
        options = completion_options(binding, binding['model'], binding['base_url'])
        async with cloud.client() as client:
            client.max_retries = 0
            response = await client.chat.completions.create(
                model=binding['model'], messages=messages, temperature=.3,
                response_format={'type': 'json_object'}, **options)
    raw = response.choices[0].message.content or ''
    usage = {'input_tokens': response.usage.prompt_tokens if response.usage else None,
             'output_tokens': response.usage.completion_tokens if response.usage else None,
             'total_tokens': response.usage.total_tokens if response.usage else None}
    result = {'raw': raw, 'usage': usage, 'finish_reason': response.choices[0].finish_reason}
    try:
        if result['finish_reason'] == 'length':
            raise ValueError('Output truncated at configured limit.')
        result['decision'] = schema.model_validate_json(raw).model_dump()
        result['ok'] = True
    except Exception as error:
        result.update(ok=False, error=runtime.safe_error(error)[:300])
    return result


def summarize(results, primary, elapsed, mode):
    usages = [call.get('usage', {}) for row in results for call in row.get('calls', [])]
    totals = {key: sum(u.get(key) or 0 for u in usages) for key in ('input_tokens', 'output_tokens', 'total_tokens')}
    return {'mode': mode, 'model': primary['model'], 'thinking': primary.get('thinking'),
            'papers': len(results), 'failed': sum(not r.get('ok') for r in results),
            'average_seconds': round(statistics.mean(r['seconds'] for r in results), 3),
            'elapsed_seconds': round(elapsed, 3), 'usage': totals,
            'average_total_tokens': round(totals['total_tokens'] / len(results), 1),
            'cloud_calls': len(usages), 'usage_known_calls': sum(u.get('total_tokens') is not None for u in usages),
            'first_pass_exact_names': sum(r.get('directory', {}).get('name_in_catalog', False) for r in results),
            'first_pass_path_consistent': sum(r.get('directory', {}).get('path_consistent_match', False) for r in results),
            'assigned': sum(bool(r.get('final_key')) for r in results),
            'assignment_methods': dict(Counter(r.get('method', 'direct') for r in results))}


def save(path, summary, rows, **extra):
    Path(path).write_text(json.dumps({'created_at': now(), 'summary': summary, **extra,
                                    'results': sorted(rows, key=lambda r: r['id'])}, ensure_ascii=False, indent=2), encoding='utf-8')


async def evaluate(args):
    papers = json.loads(Path(args.sample).read_text(encoding='utf-8'))['results']
    prompt = Path(args.prompt).read_text(encoding='utf-8')
    align_prompt = Path(args.align_prompt).read_text(encoding='utf-8')
    config = runtime.configuration()
    primary = runtime.resolve(config['routes']['classify']['primary'], config)
    if primary['kind'] != 'cloud':
        raise ValueError('The selected classification model is not cloud.')
    original_dir = settings().data_dir
    entries = index_entries()
    by_system = {system: [e for e in entries if e['system'] == system] for system in ('MSC2020', 'CCS2012')}
    by_key = {e['key']: e for e in entries}
    semaphore = asyncio.Semaphore(args.concurrency)
    start = time.perf_counter()
    first = []
    try:
        with tempfile.TemporaryDirectory(prefix='topic-route-evaluation-') as directory:
            settings().data_dir = Path(directory)
            init_db(recover=False)
            with runtime.model_snapshot(config):
                async def initial(paper):
                    async with semaphore:
                        before = time.perf_counter()
                        system = 'MSC2020' if (paper.get('category') or '').startswith('math.') else 'CCS2012'
                        row = {k: paper.get(k) for k in ('id', 'title', 'abstract', 'category', 'venue')}
                        row.update(system=system, calls=[])
                        messages = [{'role': 'system', 'content': prompt},
                                    {'role': 'user', 'content': '指定体系：' + system + '\n标题：' + paper['title'] + '\n摘要：' + (paper.get('abstract') or '')}]
                        try:
                            response = await cloud_request(primary, messages, CoreDecision)
                            row['calls'].append(response)
                            row['ok'] = response['ok']
                            if response['ok']:
                                row['decision'] = response['decision']
                                matches, row['directory'] = directory_matches(row['decision'], by_system[system])
                                row['evidence_exact'] = quote_valid(row, row['decision']['evidence'])
                            else:
                                row['error'] = response['error']
                        except Exception as error:
                            row.update(ok=False, error=runtime.safe_error(error)[:300])
                        row['seconds'] = round(time.perf_counter() - before, 3)
                        first.append(row)
                        if len(first) % 8 == 0 or len(first) == len(papers):
                            print(json.dumps({'stage': 'direct_v2', 'completed': len(first), 'total': len(papers), 'failed': sum(not r['ok'] for r in first)}), flush=True)
                await asyncio.gather(*(initial(paper) for paper in papers))
                first_elapsed = time.perf_counter() - start
                direct_summary = summarize(first, primary, first_elapsed, 'direct_v2')
                save(args.direct_output, direct_summary, first, prompt_file=args.prompt, concurrency=args.concurrency)
                print(json.dumps(direct_summary), flush=True)

                alignment_start = time.perf_counter()
                source_cache = original_dir / 'topic_candidates.sqlite3'
                identity = json.dumps(runtime.embedding_identity(config))
                with sqlite3.connect(source_cache.as_uri() + '?mode=ro', uri=True) as source:
                    stored = source.execute("SELECT value FROM metadata WHERE name='identity'").fetchone()
                    if not stored or stored[0] != identity:
                        raise ValueError('Existing taxonomy vector cache does not match the configured model; no rebuild performed.')
                    with sqlite3.connect(Path(directory) / 'topic_candidates.sqlite3') as dest:
                        source.backup(dest)
                # Batch all first-pass core descriptions. No full-catalog rebuild.
                successful = [r for r in first if r['ok']]
                vectors = []
                embedding_start = time.perf_counter()
                for offset in range(0, len(successful), 32):
                    batch = successful[offset:offset+32]
                    vectors.extend(await runtime.embed([r['decision']['core_topic_en'] + '\n' + r['title'] + '\n' + r['decision']['reason'] for r in batch]))
                embedding_seconds = time.perf_counter() - embedding_start
                if len(vectors) != len(successful):
                    raise ValueError('Embedding count mismatch.')
                vector_map = dict(zip((r['id'] for r in successful), vectors))
                aligned = []
                fallback_count = 0
                async def align(row):
                    nonlocal fallback_count
                    async with semaphore:
                        before = time.perf_counter()
                        result = {**row, 'calls': list(row['calls'])}
                        result.update(method='unresolved', final_key=None, final_label=None, final_path=None)
                        try:
                            if not row['ok']:
                                raise ValueError(row['error'])
                            decision = row['decision']
                            vector = vector_map[row['id']]
                            norm = math.sqrt(sum(x*x for x in vector))
                            if not norm or not all(math.isfinite(x) for x in vector) or len(vector) != config['embedding_dim']:
                                raise ValueError('Invalid query embedding.')
                            table = 'msc_vec' if row['system'] == 'MSC2020' else 'ccs_vec'
                            with cache() as db:
                                nearest = db.execute(f'SELECT e.standard_key,v.distance FROM {table} v JOIN entries e ON e.id=v.rowid WHERE v.embedding MATCH ? AND k=200 ORDER BY v.distance', (pack([x/norm for x in vector]),)).fetchall()
                            scores = {r['standard_key']: 1-r['distance']**2/2 for r in nearest}
                            query = {'title': decision['core_topic_en'], 'abstract': row['title'] + '\n' + decision['reason']}
                            options = retrieve(query, by_system[row['system']], 20, scores)
                            matched, diagnostics = directory_matches(decision, by_system[row['system']])
                            best_score = max(scores.values())
                            safe = [e for e in matched if declared_path_matches(e, decision['path_en'])
                                    and scores.get(e['key'], -1) >= best_score-.05
                                    and not (e['system'] == 'CCS2012' and len(e['path'].split(' › ')) <= 3)]
                            choice = max(safe, key=lambda e: scores.get(e['key'], -1)) if safe else None
                            reason = decision['reason']
                            evidence = decision['evidence']
                            if choice:
                                result['method'] = 'validated_code_or_name'
                            else:
                                ranked = []
                                seen = set()
                                for key, score in sorted(scores.items(), key=lambda v: -v[1]):
                                    entry = by_key[key]
                                    if concept(entry) in seen:
                                        continue
                                    seen.add(concept(entry)); ranked.append((score, entry))
                                top, second = ranked[:2]
                                if top[0] >= .65 and top[0]-second[0] >= .05 and concept(top[1]) == concept(options[0]):
                                    choice = top[1]
                                    result['method'] = 'clear_semantic_alignment'
                            result['local_diagnostics'] = {**diagnostics, 'best_similarity': round(best_score, 4),
                                                           'matched_similarities': {e['key']: round(scores.get(e['key'], -1), 4) for e in matched}}
                            if not choice:
                                fallback_count += 1
                                result['candidate_paths'] = [{'id': i+1, 'key': e['key'], 'path': e['path']} for i,e in enumerate(options)]
                                material = {'core_topic_en': decision['core_topic_en'], 'candidates': [{'id': i+1, 'path': e['path']} for i,e in enumerate(options)],
                                            'title': row['title'], 'abstract': row.get('abstract') or ''}
                                call = await cloud_request(primary, [{'role': 'system', 'content': align_prompt},
                                                                      {'role': 'user', 'content': json.dumps(material, ensure_ascii=False)}], AlignedDecision)
                                result['calls'].append(call)
                                if not call['ok']:
                                    raise ValueError(call['error'])
                                correction = call['decision']; number = correction['choice']
                                if number is not None:
                                    if number < 1 or number > len(options):
                                        raise ValueError('Returned candidate number out of range.')
                                    choice = options[number-1]
                                    result['method'] = 'one_cloud_correction'
                                    reason = correction['reason']; evidence = correction['evidence']
                                else:
                                    result['method'] = 'automatic_unmatched'
                            if choice:
                                result.update(final_key=choice['key'], final_label=choice['label'], final_path=choice['path'])
                            result.update(final_reason=reason, final_evidence=evidence,
                                          evidence_exact=quote_valid(result, evidence), ok=True)
                        except Exception as error:
                            result.update(ok=False, error=runtime.safe_error(error)[:400])
                        result['alignment_seconds'] = round(time.perf_counter() - before, 3)
                        result['seconds'] = round(row['seconds'] + result['alignment_seconds'] + embedding_seconds / max(1, len(successful)), 3)
                        aligned.append(result)
                        if len(aligned) % 8 == 0 or len(aligned) == len(first):
                            print(json.dumps({'stage': 'alignment', 'completed': len(aligned), 'total': len(first), 'extra_cloud_calls': fallback_count}), flush=True)
                await asyncio.gather(*(align(row) for row in first))
                elapsed = first_elapsed + time.perf_counter() - alignment_start
                summary = summarize(aligned, primary, elapsed, 'core_then_align')
                summary.update(extra_cloud_calls=fallback_count, embedding_model=runtime.selected('embedding')['model'],
                               embedding_batch_calls=math.ceil(len(successful)/32), embedding_seconds=round(embedding_seconds, 3),
                               standard_index_rebuilt=False, local_reused_first_pass=len(first),
                               local_thresholds={'exact_name_max_semantic_gap': .05, 'semantic_only_min_similarity': .65, 'semantic_only_min_margin': .05})
                save(args.route_output, summary, aligned, prompt_file=args.prompt, alignment_prompt_file=args.align_prompt,
                     timing='First-pass batch followed by local alignment and conditional correction batch; first responses reused for a paired comparison.')
                print(json.dumps(summary), flush=True)
    finally:
        settings().data_dir = original_dir


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sample', required=True)
    parser.add_argument('--prompt', required=True)
    parser.add_argument('--align-prompt', required=True)
    parser.add_argument('--direct-output', required=True)
    parser.add_argument('--route-output', required=True)
    parser.add_argument('--concurrency', type=int, default=4)
    asyncio.run(evaluate(parser.parse_args()))
