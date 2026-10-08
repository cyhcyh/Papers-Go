"""One-shot cloud classification, without candidate lists or embedding calls.

Only evaluation artifacts are saved. Model config is captured before temporary
DB setup; API credentials never enter the output and production labels stay put.
"""
import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import re
import statistics
import sys
import tempfile
import time
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pydantic import BaseModel, ConfigDict, Field
from app.config import settings, now
from app.db import init_db
from app.llm import runtime
from app.llm.provider import cloud, completion_options
from app.llm.controls import capabilities
from app.llm.thinking import clean_answer
from app.standard_topics import catalog


class DirectDecision(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    system: str
    code: str | None
    name_en: str = Field(min_length=1, max_length=250)
    path_en: list[str] = Field(max_length=10)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=200)
    evidence: str = Field(max_length=240)


def normalized(value):
    return re.sub(r'[^a-z0-9]+', ' ', unicodedata.normalize('NFKC', value).casefold()).strip()


def verify_directory(decision, expected_system):
    entries = [e for e in catalog().values() if e['system'] == expected_system and e['parent']]
    code = (decision.code or '').removeprefix(expected_system + ':').strip()
    coded = [e for e in entries if code and (e['code'] == code or expected_system == 'CCS2012' and e['code'].split('.')[-1] == code)]
    named = [e for e in entries if normalized(e['label']) == normalized(decision.name_en)]
    exact = [e for e in coded if e in named]
    matched = exact or named
    method = 'code_and_name' if exact else 'exact_name' if named else 'unresolved'
    if matched:
        path_terms = set(normalized(' '.join(decision.path_en)).split())
        matched.sort(key=lambda e: (-len(path_terms & set(normalized(e['path']).split())), e['key']))
    return {
        'code_in_catalog': bool(coded), 'code_name_consistent': bool(exact),
        'name_in_catalog': bool(named), 'resolution': method,
        'resolved_key': matched[0]['key'] if matched else None,
        'resolved_label': matched[0]['label'] if matched else None,
        'resolved_path': matched[0]['path'] if matched else None,
        'code_entries': [{'key': e['key'], 'label': e['label'], 'path': e['path']} for e in coded[:5]],
        'system_matches': decision.system == expected_system,
    }


async def evaluate(args):
    source = json.loads(Path(args.sample).read_text(encoding='utf-8'))
    papers = source['results'][:args.limit] if args.limit else source['results']
    prompt = Path(args.prompt).read_text(encoding='utf-8')
    config = runtime.configuration()
    original_dir = settings().data_dir
    primary = runtime.resolve(config['routes']['classify']['primary'], config)
    if args.model:
        primary['model'] = args.model
    if args.thinking:
        primary['thinking'] = args.thinking
    if primary['kind'] != 'cloud':
        raise RuntimeError('The selected classification model is not a cloud connection.')
    if args.thinking and args.thinking not in capabilities(primary)['thinking_modes']:
        raise RuntimeError('The connection does not support the requested thinking mode.')
    if args.thinking == 'on':
        primary['reasoning_effort'] = 'auto'
    request_binding = {**primary, 'feature': 'classify'}
    test_options = completion_options(request_binding, primary['model'], primary['base_url'])
    if args.max_output_tokens:
        test_options['max_tokens'] = args.max_output_tokens
    results = []
    semaphore = asyncio.Semaphore(args.concurrency)
    started = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix='direct-topic-evaluation-') as directory:
            settings().data_dir = Path(directory)
            init_db(recover=False)
            with runtime.model_snapshot(config):
                async def process(paper):
                    async with semaphore:
                        before = time.perf_counter()
                        system = 'MSC2020' if (paper.get('category') or '').startswith('math.') else 'CCS2012'
                        row = {k: paper.get(k) for k in ('id', 'title', 'abstract', 'category', 'venue')}
                        row['expected_system'] = system
                        messages = [
                            {'role': 'system', 'content': prompt},
                            {'role': 'user', 'content': '指定标准体系：' + system + '\n论文标题：' + paper['title'] + '\n摘要：' + (paper.get('abstract') or '')},
                        ]
                        binding = {**primary, 'feature': 'classify'}
                        try:
                            with runtime.bind(binding):
                                options = dict(test_options)
                                async with cloud.client() as client:
                                    client.max_retries = 0
                                    response = await client.chat.completions.create(
                                        model=binding['model'], messages=messages, temperature=.3,
                                        response_format={'type': 'json_object'}, **options,
                                    )
                                row['usage'] = {
                                    'input_tokens': response.usage.prompt_tokens if response.usage else None,
                                    'output_tokens': response.usage.completion_tokens if response.usage else None,
                                    'total_tokens': response.usage.total_tokens if response.usage else None,
                                }
                                details = getattr(response.usage, 'completion_tokens_details', None)
                                row['reasoning_tokens'] = getattr(details, 'reasoning_tokens', None)
                                reasoning = getattr(response.choices[0].message, 'reasoning_content', None)
                                row['reasoning_observed'] = bool(reasoning)
                                row['reasoning_characters'] = len(reasoning) if isinstance(reasoning, str) else 0
                                row['finish_reason'] = response.choices[0].finish_reason
                                raw = clean_answer(response.choices[0].message.content or '')
                                row['raw'] = raw
                                if row['finish_reason'] == 'length':
                                    raise ValueError('Output truncated at configured token limit.')
                                decision = DirectDecision.model_validate_json(raw)
                                row['decision'] = decision.model_dump()
                                row.update(verify_directory(decision, system))
                                evidence = ' '.join(unicodedata.normalize('NFKC', decision.evidence).casefold().split())
                                text = ' '.join(unicodedata.normalize('NFKC', paper['title'] + '\n' + (paper.get('abstract') or '')).casefold().split())
                                row['evidence_exact'] = len(evidence) >= 8 and evidence in text
                                row['ok'] = True
                        except Exception as error:
                            row.update(ok=False, error=runtime.safe_error(error)[:400])
                        row['seconds'] = round(time.perf_counter() - before, 3)
                        results.append(row)
                        if len(results) % 8 == 0 or len(results) == len(papers):
                            print(json.dumps({'completed': len(results), 'total': len(papers), 'failed': sum(not r['ok'] for r in results), 'elapsed_seconds': round(time.perf_counter() - started, 2)}), flush=True)

                await asyncio.gather(*(process(paper) for paper in papers))
    finally:
        settings().data_dir = original_dir
    usage = {key: sum((r.get('usage', {}).get(key) or 0) for r in results) for key in ('input_tokens', 'output_tokens', 'total_tokens')}
    summary = {
        'model': primary['model'], 'thinking': primary.get('thinking'), 'concurrency': args.concurrency,
        'reasoning_effort': primary.get('reasoning_effort'),
        'max_output_tokens': test_options.get('max_tokens'),
        'reasoning_observed_calls': sum(r.get('reasoning_observed', False) for r in results),
        'reasoning_tokens_known_calls': sum(r.get('reasoning_tokens') is not None for r in results),
        'reasoning_tokens': sum(r.get('reasoning_tokens') or 0 for r in results),
        'papers': len(results), 'failed': sum(not r['ok'] for r in results),
        'code_in_catalog': sum(r.get('code_in_catalog', False) for r in results),
        'name_in_catalog': sum(r.get('name_in_catalog', False) for r in results),
        'code_name_consistent': sum(r.get('code_name_consistent', False) for r in results),
        'exact_directory_resolved': sum(bool(r.get('resolved_key')) and r.get('system_matches', False) for r in results),
        'evidence_exact': sum(r.get('evidence_exact', False) for r in results),
        'resolution_methods': dict(Counter(r.get('resolution', 'failed') for r in results)),
        'average_seconds': round(statistics.mean(r['seconds'] for r in results), 3) if results else None,
        'elapsed_seconds': round(time.perf_counter() - started, 2),
        'usage': usage,
        'average_total_tokens': round(usage['total_tokens'] / len(results), 1) if results else None,
        'sdk_retries': 0, 'candidate_lists_sent': False, 'embedding_calls': 0,
    }
    Path(args.output).write_text(json.dumps({'created_at': now(), 'prompt_file': str(Path(args.prompt)), 'summary': summary, 'results': sorted(results, key=lambda r: r['id'])}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sample', required=True)
    parser.add_argument('--prompt', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--model')
    parser.add_argument('--thinking', choices=('on', 'off', 'auto'))
    parser.add_argument('--max-output-tokens', type=int)
    asyncio.run(evaluate(parser.parse_args()))
