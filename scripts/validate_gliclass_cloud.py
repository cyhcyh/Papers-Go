"""One non-thinking DeepSeek choice from GLiClass-routed standard subtrees."""
import argparse
import asyncio
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from pydantic import BaseModel, ConfigDict, Field
from app.config import settings, now
from app.db import init_db
from app.llm import runtime
from app.llm.provider import cloud, completion_options
from app.llm.thinking import clean_answer


class Choice(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    choice: int = Field(ge=1)
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=300)
    evidence: str = Field(max_length=400)


async def main(args):
    sample = json.loads(Path(args.sample).read_text(encoding='utf-8'))
    prompt = Path(args.prompt).read_text(encoding='utf-8')
    config = runtime.configuration()
    primary = runtime.resolve(config['routes']['classify']['primary'], config)
    primary.update(model='deepseek-v4.1-flash', thinking='off', reasoning_effort='auto', feature='classify')
    if primary['kind'] != 'cloud':
        raise ValueError('Classification connection must be cloud for this test.')
    original_dir = settings().data_dir
    results = []
    semaphore = asyncio.Semaphore(args.concurrency)
    started = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory(prefix='gliclass-cloud-evaluation-') as folder:
            settings().data_dir = Path(folder)
            init_db(recover=False)
            with runtime.model_snapshot(config):
                async def process(paper):
                    async with semaphore:
                        before = time.perf_counter()
                        row = dict(paper)
                        options = row['candidates']
                        material = {'system': row['system'], 'title': row['title'], 'abstract': row.get('abstract') or '',
                                    'candidates': [{'id': i+1, 'path': e['path']} for i,e in enumerate(options)]}
                        messages = [{'role': 'system', 'content': prompt},
                                    {'role': 'user', 'content': json.dumps(material, ensure_ascii=False)}]
                        try:
                            with runtime.bind(primary):
                                request_options = completion_options(primary, primary['model'], primary['base_url'])
                                async with cloud.client() as client:
                                    client.max_retries = 0
                                    response = await client.chat.completions.create(model=primary['model'], messages=messages,
                                        temperature=.3, response_format={'type': 'json_object'}, **request_options)
                            row['usage'] = {'input_tokens': response.usage.prompt_tokens, 'output_tokens': response.usage.completion_tokens,
                                            'total_tokens': response.usage.total_tokens}
                            row['reasoning_observed'] = bool(getattr(response.choices[0].message, 'reasoning_content', None))
                            row['finish_reason'] = response.choices[0].finish_reason
                            row['raw'] = clean_answer(response.choices[0].message.content or '')
                            if row['finish_reason'] == 'length':
                                raise ValueError('Output truncated.')
                            decision = Choice.model_validate_json(row['raw']).model_dump()
                            if decision['choice'] > len(options):
                                raise ValueError('Choice outside candidate list.')
                            entry = options[decision['choice']-1]
                            row.update(decision=decision, final_key=entry['key'], final_label=entry['label'], final_path=entry['path'], ok=True)
                            evidence = ' '.join(decision['evidence'].casefold().split())
                            text = ' '.join((row['title']+'\n'+row['abstract']).casefold().split())
                            row['evidence_exact'] = len(evidence) >= 8 and evidence in text
                        except Exception as error:
                            row.update(ok=False, error=runtime.safe_error(error)[:350])
                        row['cloud_seconds'] = round(time.perf_counter() - before, 3)
                        row['seconds'] = round(row['coarse_seconds'] + row['cloud_seconds'], 3)
                        results.append(row)
                        if len(results) % 8 == 0 or len(results) == len(sample['results']):
                            print(json.dumps({'stage': 'cloud', 'completed': len(results), 'total': len(sample['results']),
                                              'failed': sum(not r['ok'] for r in results)}), flush=True)
                await asyncio.gather(*(process(paper) for paper in sample['results']))
    finally:
        settings().data_dir = original_dir
    usage = {k: sum(r.get('usage', {}).get(k, 0) for r in results) for k in ('input_tokens', 'output_tokens', 'total_tokens')}
    summary = {'coarse_model': sample['summary']['model'], 'coarse_device': sample['summary']['device'],
               'cloud_model': primary['model'], 'thinking': 'off', 'concurrency': args.concurrency,
               'papers': len(results), 'failed': sum(not r['ok'] for r in results),
               'assigned': sum(bool(r.get('final_key')) for r in results), 'cloud_calls': len(results),
               'reasoning_observed_calls': sum(r.get('reasoning_observed', False) for r in results),
               'average_coarse_seconds': sample['summary']['average_seconds'],
               'average_cloud_seconds': round(statistics.mean(r['cloud_seconds'] for r in results), 3),
               'average_seconds': round(statistics.mean(r['seconds'] for r in results), 3),
               'cloud_batch_seconds': round(time.perf_counter() - started, 3),
               'usage': usage, 'average_total_tokens': round(usage['total_tokens']/len(results), 1),
               'average_candidates': sample['summary']['average_candidates'],
               'maximum_candidates': sample['summary']['maximum_candidates'],
               'embedding_calls': 0, 'sdk_retries': 0}
    Path(args.output).write_text(json.dumps({'created_at': now(), 'summary': summary,
                    'coarse_summary': sample['summary'], 'results': sorted(results, key=lambda r:r['id'])}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sample', required=True)
    parser.add_argument('--prompt', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--concurrency', type=int, default=4)
    asyncio.run(main(parser.parse_args()))
