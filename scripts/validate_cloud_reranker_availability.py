"""Small cloud reranker experiment using saved credentials, without DB writes."""
import asyncio
import json
from pathlib import Path
import sqlite3
import sys
import time
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
sys.stdout.reconfigure(encoding='utf-8')

import httpx
from app.llm.secrets import decrypt_configuration


async def main():
    with sqlite3.connect((ROOT / 'data/papers.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        config = decrypt_configuration(json.loads(db.execute(
            "SELECT value FROM app_settings WHERE name='models'").fetchone()[0]))
    choice = config['routes']['classify']['primary']
    connection = next(c for c in config['connections'] if c['id'] == choice['connection_id'])
    origin = urlsplit(connection['base_url'])
    if origin.scheme != 'https' or not (origin.hostname or '').endswith('.cn-beijing.maas.aliyuncs.com'):
        raise ValueError('This experiment requires the existing Beijing Model Studio connection.')
    if not connection.get('api_key'):
        raise ValueError('No saved API key on the existing connection.')
    endpoint = urlunsplit((origin.scheme, origin.netloc, '/compatible-api/v1/reranks', '', ''))
    entries = json.loads((ROOT / 'backend/app/resources/standard_catalog.json').read_text(encoding='utf-8'))
    sample = json.loads((ROOT / 'artifacts/classification-gliclass-coarse-results-20261003.json').read_text(encoding='utf-8'))
    selected = [p for p in sample['results'] if p['id'] in (5284, 5488, 1319, 1666)]
    instruction = (
        'Determine whether the candidate scientific classification includes the primary research problem '
        'and main contribution of the paper. A broad category is relevant if it contains the primary '
        'research subfield. Focus on the main contribution rather than incidental methods, applications, '
        'symbols or mentioned concepts. Rank candidate classifications by their suitability for the primary subject.'
    )
    results = []
    async with httpx.AsyncClient(timeout=35, follow_redirects=False) as client:
        for paper in selected:
            roots = [e for e in entries if e['system'] == paper['system'] and not e['parent']]
            body = {'model': 'qwen3-rerank', 'query': paper['title'] + '\n' + paper['abstract'],
                    'documents': [e['code'] + ' ' + e['label'] for e in roots], 'top_n': 3,
                    'instruct': instruction}
            started = time.perf_counter()
            row = {'id': paper['id'], 'title': paper['title'], 'system': paper['system'],
                   'candidates': len(roots)}
            try:
                response = await client.post(endpoint, json=body,
                    headers={'Authorization': 'Bearer ' + connection['api_key']})
                row['http_status'] = response.status_code
                payload = response.json()
                if not response.is_success:
                    row.update(ok=False, error_code=payload.get('code') or payload.get('error', {}).get('code'))
                else:
                    scores = payload.get('results') or payload.get('output', {}).get('results')
                    if not scores:
                        raise ValueError('No rerank results in response.')
                    row.update(ok=True, model=payload.get('model', body['model']), usage=payload.get('usage', {}),
                        top=[{'key': roots[item['index']]['key'], 'label': roots[item['index']]['label'],
                              'score': item['relevance_score']} for item in scores])
            except Exception as error:
                row.update(ok=False, error_type=type(error).__name__)
            row['seconds'] = round(time.perf_counter() - started, 3)
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if not row['ok']:
                break
    result = {'scope': 'Cloud availability and top-3 root routing only; not a full classification evaluation.',
              'provider': 'Alibaba Cloud Model Studio', 'requested_model': 'qwen3-rerank',
              'open_weight_parameter_size': 'Not specified by the provider documentation.',
              'instruction': instruction, 'results': results}
    output = ROOT / 'artifacts/classification-cloud-reranker-smoke-20261003.json'
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'report': str(output), 'calls': len(results),
                      'successful': sum(r['ok'] for r in results)}), flush=True)


if __name__ == '__main__':
    asyncio.run(main())
