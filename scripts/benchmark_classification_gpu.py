"""Compare the current classifier on CPU/GPU without writing paper labels.

Run inside the app container via stdin, so SQLite stays on its Linux host.
Both endpoints must already contain the same model and use identical settings.
"""
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.db import one, rows
from app.llm import runtime
from app.pipeline.classify import classify_paper


PAPER_IDS = [5272, 152, 196, 5273]
OUTPUT = Path('/data/benchmarks/classification-cpu-gpu-20261001.json')


class PromptCaptured(Exception):
    pass


async def capture_prompt(paper):
    """Stop the real classifier before it can modify paper_topics or papers."""
    captured = {}
    original = runtime.complete

    async def intercept(feature, messages, **kwargs):
        captured.update(messages=messages, schema=kwargs['schema'])
        raise PromptCaptured()

    runtime.complete = intercept
    try:
        await classify_paper(paper)
    except PromptCaptured:
        return captured
    finally:
        runtime.complete = original
    raise RuntimeError('Sample has no candidate topics')


def main():
    binding = runtime.selected('classify')
    if binding['kind'] != 'ollama':
        raise RuntimeError('GPU benchmark requires an Ollama classification route')
    samples = []
    topics = {t['id']: t['name_en'] for t in rows("SELECT id,name_en FROM topics WHERE status='active'")}
    for ident in PAPER_IDS:
        paper = one('SELECT * FROM papers WHERE id=?', (ident,))
        captured = asyncio.run(capture_prompt(paper))
        from app.taxonomy import scoped_topics
        allowed = {t['id'] for t in scoped_topics(paper, rows("SELECT * FROM topics WHERE status='active'"))}
        samples.append((paper, captured, allowed))
    report = {
        'created_at': datetime.now(timezone.utc).isoformat(),
        'model': binding['model'], 'paper_ids': PAPER_IDS,
        'settings': {'temperature': .1, 'num_ctx': 8192, 'think': False},
        'writes_paper_labels': False, 'runs': [],
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    recheck_gpu = '--gpu-recheck' in sys.argv
    if recheck_gpu:
        report = json.loads(OUTPUT.read_text(encoding='utf-8'))
        if report['model'] != binding['model'] or report['paper_ids'] != PAPER_IDS:
            raise RuntimeError('Recheck must use the original model and samples')

    def save():
        OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    with httpx.Client(timeout=180) as client:
        endpoints = [('GPU steady', 'http://shualunwen-ollama-gpu-test:11434')] if recheck_gpu else [('CPU', binding['base_url']), ('GPU', 'http://shualunwen-ollama-gpu-test:11434')]
        for name, endpoint in endpoints:
            endpoint = endpoint.rstrip('/')
            print(json.dumps({'phase': name, 'status': 'loading model'}, ensure_ascii=False), flush=True)
            started = time.perf_counter()
            response = client.post(endpoint + '/api/generate', json={
                'model': binding['model'], 'prompt': '', 'stream': False,
                'keep_alive': '5m', 'options': {'temperature': .1, 'num_ctx': 8192},
            })
            response.raise_for_status()
            run = {'processor': name, 'warmup_seconds': round(time.perf_counter()-started, 3), 'samples': []}
            report['runs'].append(run)
            for paper, captured, allowed in samples:
                started = time.perf_counter()
                item = {'paper_id': paper['id'], 'title': paper['title'], 'primary_category': paper['primary_category']}
                try:
                    response = client.post(endpoint + '/api/chat', json={
                        'model': binding['model'], 'messages': captured['messages'],
                        'stream': False, 'format': captured['schema'], 'think': False,
                        'keep_alive': '5m', 'options': {'temperature': .1, 'num_ctx': 8192},
                    })
                    response.raise_for_status()
                    payload = response.json()
                    result = json.loads(payload['message']['content'])
                    selected = result['topics']
                    valid = isinstance(selected, list) and len(selected) <= 3 and all(
                        isinstance(t, dict) and t.get('id') in allowed and
                        isinstance(t.get('confidence'), (int, float)) and 0 <= t['confidence'] <= 1
                        for t in selected
                    ) and 'new_topic_hint' in result
                    item.update(ok=valid, result=result, topics=[
                        {'name': topics[t['id']], 'confidence': t['confidence']}
                        for t in selected if t.get('id') in allowed
                    ])
                    item['metrics'] = {key: payload.get(key) for key in (
                        'total_duration', 'load_duration', 'prompt_eval_count',
                        'prompt_eval_duration', 'eval_count', 'eval_duration',
                    )}
                    item['inference_seconds'] = round((payload['total_duration']-payload['load_duration'])/1e9, 3)
                    item['tokens_per_second'] = round(payload['eval_count']/(payload['eval_duration']/1e9), 2)
                except Exception as error:
                    item.update(ok=False, error_type=type(error).__name__)
                item['seconds'] = round(time.perf_counter()-started, 3)
                run['samples'].append(item)
                save()
                print(json.dumps({'processor': name, **item}, ensure_ascii=False), flush=True)
            response = client.get(endpoint + '/api/ps')
            response.raise_for_status()
            run['loaded_models'] = response.json()['models']
            run['mean_seconds'] = round(statistics.mean(item['seconds'] for item in run['samples']), 3)
            run['median_seconds'] = round(statistics.median(item['seconds'] for item in run['samples']), 3)
            save()
            print(json.dumps({'processor': name, 'mean_seconds': run['mean_seconds'], 'median_seconds': run['median_seconds'], 'size_vram': [m.get('size_vram') for m in run['loaded_models']]}, ensure_ascii=False), flush=True)
            # Free each model after its pass; leave the original service configuration intact.
            client.post(endpoint + '/api/generate', json={'model': binding['model'], 'keep_alive': 0}).raise_for_status()
    report['speedup_mean'] = round(report['runs'][0]['mean_seconds']/report['runs'][-1]['mean_seconds'], 2)
    save()
    print(json.dumps({'report': str(OUTPUT), 'speedup_mean': report['speedup_mean']}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
