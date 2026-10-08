"""Bounded interest text cache; no per-paper LLM calls and no new service."""
import asyncio
import hashlib
import json
from .. import prompts
from ..config import now
from ..db import connect, dumps
from ..llm import runtime as models
from ..logs import event
from .form import parse_form

PROMPT_NAME = 'interest_retrieval'


def prompt_revision():
    return hashlib.sha256(prompts.get(PROMPT_NAME).encode()).hexdigest()


def validate_response(response, originals):
    values = response['interests']
    if [v['original'] for v in values] != originals:
        raise ValueError('Interest descriptions do not match the input.')
    queries = []
    for original, item in zip(originals, values):
        query = item['query']
        if not isinstance(query, str) or not query.startswith(original) or len(query) > 800:
            raise ValueError('Invalid interest description.')
        if not query[len(original):].strip() or len(query[len(original):].split()) > 65:
            raise ValueError('Interest description is empty or too long.')
        queries.append(query)
    return queries


def query_payload(content, entries):
    originals = [e['text'] for e in entries]
    form = parse_form(content)
    return {'interests': originals,
               'long_term': [e['text'] for e in form['long_term'] if e['text'] in originals],
               'temporary': [e['text'] for e in form['temporary'] if e['text'] in originals]}


async def search_queries(content, entries, *, strict=False):
    originals = [e['text'] for e in entries]
    payload = query_payload(content, entries)
    # Cache descriptions independently of vector models and interest weights.
    # Updating an embedding model therefore reuses the meaning, then re-embeds it.
    cache_key = hashlib.sha256((prompt_revision() + dumps(payload)).encode()).hexdigest()
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS interest_retrieval_cache(
            cache_key TEXT PRIMARY KEY, queries TEXT NOT NULL, created_at TEXT NOT NULL)''')
        saved = db.execute('SELECT queries FROM interest_retrieval_cache WHERE cache_key=?', (cache_key,)).fetchone()
    if saved:
        return json.loads(saved['queries'])
    if not models.configured('interest_init'):
        if strict:
            raise ValueError('Interest description model not configured')
        return originals
    try:
        response = await asyncio.wait_for(models.complete('interest_init', [
            {'role': 'system', 'content': prompts.get(PROMPT_NAME)},
            {'role': 'user', 'content': dumps(payload)}
        ], json_mode=True, cache_seconds=0), timeout=60)
        queries = validate_response(response, originals)
    except Exception as error:
        event('model', '兴趣整理失败，沿用原始兴趣', level='warning', job='interest_init', error_type=type(error).__name__)
        if strict:
            raise
        return originals
    with connect() as db:
        db.execute('INSERT OR REPLACE INTO interest_retrieval_cache VALUES(?,?,?)', (cache_key, dumps(queries), now()))
        db.execute('''DELETE FROM interest_retrieval_cache WHERE cache_key NOT IN
            (SELECT cache_key FROM interest_retrieval_cache ORDER BY created_at DESC LIMIT 200)''')
    return queries
