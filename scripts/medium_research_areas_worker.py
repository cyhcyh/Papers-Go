"""Run via docker exec, with JSON stdin and results stdout. Production DB is read-only."""
import asyncio
from collections import Counter
from contextvars import ContextVar
import json
import math
from pathlib import Path
import sqlite3
import statistics
import sys
import time

import httpx
from openai import AsyncOpenAI

from app.config import settings
from app.llm import runtime
from app.llm.provider import clean_answer, completion_options
from app.llm.secrets import decrypt_configuration
from app.pipeline import topic_decision
from app import prompts
from app.topic_retrieval import retrieve
from app.topic_semantics import entry_text, paper_text


payload = json.loads(sys.stdin.buffer.read().decode('utf-8'))
trace_context = ContextVar('experiment_trace')


def unit(vector, dimension):
    if len(vector) != dimension or not all(math.isfinite(v) for v in vector):
        raise ValueError('Invalid embedding dimension or values.')
    norm = math.sqrt(sum(v * v for v in vector))
    if not norm:
        raise ValueError('Zero embedding.')
    return [v / norm for v in vector]


async def main():
    database = sqlite3.connect((settings().data_dir / 'papers.sqlite3').resolve().as_uri() + '?mode=ro', uri=True)
    config = decrypt_configuration(json.loads(database.execute("SELECT value FROM app_settings WHERE name='models'").fetchone()[0]))
    saved = database.execute("SELECT value FROM app_settings WHERE name='prompts'").fetchone()
    original_prompt = (json.loads(saved[0]) if saved else {}).get('classify', prompts.DEFAULTS['classify']['text'])
    database.close()
    binding = {**runtime.resolve(config['routes']['classify']['primary'], config), 'feature': 'classify'}
    embedding = runtime.resolve(config['routes']['embedding']['primary'], config)
    if binding['kind'] != 'cloud' or embedding['kind'] != 'ollama':
        raise ValueError('Expected saved cloud classification and local Ollama embedding routes.')
    dimension = config['embedding_dim']
    # The only instruction edits describe the new input taxonomy and replace
    # the old-format example ID. Decision contract and audit logic are reused.
    adapted_prompt = original_prompt.replace('官方 MSC2020 或 ACM CCS2012', '本次提供的中粒度研究方向表').replace('MSC2020:05D10', 'RA-MATH-060')
    prompts.get = lambda feature: adapted_prompt if feature == 'classify' else prompts.DEFAULTS[feature]['text']
    topic_decision.event = lambda *args, **kwargs: trace_context.get()['events'].append({'message': args[1] if len(args) > 1 else '', **kwargs})
    entries = [{
        'key': area['id'], 'system': 'ResearchAreas', 'code': area['id'], 'label': area['name'],
        'parent': None, 'discipline': area['discipline'], 'description': area['description'],
        'display_path': area['discipline'] + ' › ' + area['name'],
        'path': area['discipline'] + ' › ' + area['name'] + '\nResearch area description: ' + area['description'],
    } for area in payload['areas']]
    pools = {discipline: [e for e in entries if e['discipline'] == discipline] for discipline in ['Mathematics', 'Computer Science']}
    references = {row['id']: row for row in payload['reference']['cases']}
    local_lock = asyncio.Lock()
    concurrency = asyncio.Semaphore(payload['concurrency'])
    traces = []
    results = list(payload.get('prior_results', []))
    requests = []

    async with httpx.AsyncClient(timeout=180) as ollama_client, AsyncOpenAI(
        base_url=binding['base_url'], api_key=binding['api_key'], max_retries=0,
        http_client=httpx.AsyncClient(timeout=120),
    ) as cloud_client:
        async def embed(texts):
            async with local_lock:
                started = time.perf_counter()
                response = await ollama_client.post(embedding['base_url'].rstrip('/') + '/api/embed',
                    json={'model': embedding['model'], 'input': texts, 'truncate': True})
                response.raise_for_status()
                vectors = response.json()['embeddings']
                if len(vectors) != len(texts):
                    raise ValueError('Embedding count mismatch.')
                requests.append({'documents': len(texts), 'seconds': round(time.perf_counter() - started, 3)})
                return [unit(vector, dimension) for vector in vectors]

        async def complete(feature, messages, json_mode=False, validate=None, schema=None, **kwargs):
            trace = trace_context.get()
            call = {'number': len(trace['calls']) + 1, 'stage': 'scope_check' if '待审核的原建议：' in messages[0]['content'] else 'initial',
                    'prompt_characters': sum(len(m['content']) for m in messages)}
            trace['calls'].append(call)
            started = time.perf_counter()
            try:
                options = completion_options(binding, binding['model'], binding['base_url'])
                response = await cloud_client.chat.completions.create(model=binding['model'], messages=messages,
                    temperature=.3, response_format={'type': 'json_object'}, **options)
                usage = response.usage
                call['usage'] = {'input_tokens': usage.prompt_tokens, 'output_tokens': usage.completion_tokens, 'total_tokens': usage.total_tokens} if usage else None
                message = response.choices[0].message
                call['reasoning_observed'] = bool(getattr(message, 'reasoning_content', None))
                call['finish_reason'] = response.choices[0].finish_reason
                if call['finish_reason'] == 'length':
                    raise ValueError('Output length limit.')
                value = json.loads(clean_answer(message.content or ''))
                call['response'] = value
                result = validate(value) if validate else value
                call['valid'] = True
                return result
            except Exception as error:
                call['error_type'] = type(error).__name__
                call['status_code'] = getattr(error, 'status_code', None)
                raise
            finally:
                call['seconds'] = round(time.perf_counter() - started, 3)

        runtime.complete = complete
        index_start = time.perf_counter()
        vectors = {}
        for start in range(0, len(entries), 32):
            batch = entries[start:start + 32]
            values = await embed([entry_text(entry) for entry in batch])
            vectors.update({entry['key']: vector for entry, vector in zip(batch, values)})
        index_seconds = round(time.perf_counter() - index_start, 3)
        print(json.dumps({'stage': 'index_ready', 'areas': len(entries), 'discipline_counts': {key: len(value) for key, value in pools.items()},
                          'model': embedding['model'], 'seconds': index_seconds}), flush=True)
        batch_start = time.perf_counter()

        async def process(paper):
            async with concurrency:
                before = time.perf_counter()
                trace = {'id': paper['id'], 'calls': [], 'events': []}
                marker = trace_context.set(trace)
                row = {**paper, 'eligible_discipline': 'Mathematics' if (paper['primary_category'] or '').startswith('math.') and not paper['venue'] else 'Computer Science'}
                try:
                    retrieval_start = time.perf_counter()
                    query = (await embed([paper_text(paper)]))[0]
                    pool = pools[row['eligible_discipline']]
                    # Every discipline has <200 areas: all its normalized cosine
                    # scores are equivalent to the current top-200 vector query.
                    scores = {entry['key']: sum(a * b for a, b in zip(query, vectors[entry['key']])) for entry in pool}
                    options = retrieve(paper, pool, limit=40, semantic_scores=scores)
                    row['retrieval_seconds'] = round(time.perf_counter() - retrieval_start, 3)
                    row['candidates'] = options
                    row['decision'] = await topic_decision.predict_topic(paper, options)
                    selected = next((entry for entry in options if entry['key'] == row['decision']['standard_key']), None)
                    row['final_name'] = selected['label'] if selected else None
                    row['first_decision'] = trace['calls'][0].get('response') if trace['calls'] else None
                    initial_key = (row['first_decision'] or {}).get('standard_key')
                    row['first_name'] = next((entry['label'] for entry in options if entry['key'] == initial_key), None)
                    ref = references[paper['id']]
                    primary = set(ref['primary_ids'])
                    related = set(ref['related_ids'])
                    available = {entry['key'] for entry in options}
                    row['primary_target_eligible'] = bool(primary & {entry['key'] for entry in pool})
                    row['primary_candidate_present'] = bool(primary & available)
                    row['primary_candidate_top10'] = bool(primary & {entry['key'] for entry in options[:10]})
                    row['first_reference_match'] = 'primary' if initial_key in primary else 'related' if initial_key in related else 'other'
                    key = row['decision']['standard_key']
                    row['final_reference_match'] = 'primary' if key in primary else 'related' if key in related else 'other'
                    row['ok'] = True
                except Exception as error:
                    row.update(ok=False, error_type=type(error).__name__, status_code=getattr(error, 'status_code', None))
                finally:
                    row['seconds'] = round(time.perf_counter() - before, 3)
                    row['calls'] = trace['calls']; row['events'] = trace['events']
                    results.append(row); traces.append(trace)
                    trace_context.reset(marker)
                    print('PAPER_JSON:' + json.dumps(row, ensure_ascii=False), flush=True)

        await asyncio.gather(*(process(paper) for paper in payload['papers']))
        elapsed = round(time.perf_counter() - batch_start, 3)

    good = [row for row in results if row['ok']]
    calls = [call for row in results for call in row['calls']]
    usage = [call['usage'] for call in calls if call.get('usage')]
    summary = {
        'papers': len(results), 'failed': len(results) - len(good), 'areas': len(entries),
        'discipline_counts': {key: len(value) for key, value in pools.items()},
        'embedding_model': embedding['model'], 'final_model': binding['model'], 'thinking': binding.get('thinking'), 'concurrency': payload['concurrency'],
        'average_seconds': round(statistics.mean(row['seconds'] for row in results), 3),
        'batch_seconds': elapsed if not payload.get('prior_results') else None,
        'resume_batch_seconds': elapsed if payload.get('prior_results') else None,
        'resumed_from_completed_papers': len(payload.get('prior_results', [])),
        'index_build_seconds': index_seconds,
        'average_retrieval_seconds': round(statistics.mean(row.get('retrieval_seconds', 0) for row in results), 3),
        'cloud_calls': len(calls), 'scope_checks': sum(call['stage'] == 'scope_check' for call in calls),
        'usage_known_calls': len(usage), 'cloud_total_tokens': sum(u['total_tokens'] for u in usage),
        'average_cloud_tokens_per_paper': round(sum(u['total_tokens'] for u in usage) / len(results), 1),
        'first_call_total_tokens': sum(row['calls'][0].get('usage', {}).get('total_tokens', 0) for row in results if row['calls']),
        'first_reference_matches': dict(Counter(row['first_reference_match'] for row in good)),
        'final_reference_matches': dict(Counter(row['final_reference_match'] for row in good)),
        'primary_target_eligible': sum(row['primary_target_eligible'] for row in good),
        'primary_candidate_coverage40': sum(row['primary_candidate_present'] for row in good),
        'primary_candidate_coverage10': sum(row['primary_candidate_top10'] for row in good),
        'review': sum(row['decision']['needs_review'] for row in good),
        'reasoning_observed_calls': sum(call.get('reasoning_observed', False) for call in calls),
    }
    report = {
        'scope': '只读生产配置，独立模型实验；未更改应用代码、论文标签、模型配置或生产分类向量索引。',
        'method': '当前Docker BGE-M3＋原关键词/语义融合算法40候选＋原DeepSeek单主题判断及范围审核；替换候选表并加入description。',
        'adaptations': ['名称＋学科路径＋原description用于向量、关键词和云端候选材料。', '原提示词的MSC/CCS来源及示例ID改为提供的研究方向表，其余决策与审核流程复用。', '85/77个条目均少于原top200向量检索上限，内存余弦与原归一化L2排名等价。', '沿用按来源先区分数学/计算机候选的范围限制，跨学科条目可能因此不可选。'],
        'limits': ['同34篇开发样本的阅读参考，非独立专家金标准。', '中粒度主题与MSC/CCS细条目的正确率口径不同。', '本次使用description，但未做不带description的消融，不能单独归因其贡献。'],
        'taxonomy_source': payload['taxonomy_source'], 'summary': summary,
        'baseline_summary': payload['baseline_summary'], 'reference': payload['reference'],
        'original_prompt': original_prompt, 'adapted_prompt': adapted_prompt,
        'embedding_requests': requests, 'results': sorted(results, key=lambda row: row['id']),
    }
    if payload.get('prior_results'):
        report['limits'].append('实验在19篇完成后被容器中断，随后复用已完成结果续跑；用量统计仅含已记录的响应，未记录的中断请求可能另有用量。不能把续跑批次耗时当作完整34篇的连续整批耗时。')
    print('RESULT_JSON:' + json.dumps(report, ensure_ascii=False), flush=True)


asyncio.run(main())
