"""Per-interest vectors live with their profile; the aggregate remains a legacy fallback."""
import base64
import json
import math
from ..db import pack, unpack


class ProfileEmbedding(bytes):
    def __new__(cls, aggregate, parts):
        value = super().__new__(cls, aggregate)
        value.parts_json = json.dumps(parts, ensure_ascii=False, separators=(',', ':'))
        return value


def combine(parts):
    if not parts:
        return None
    total = sum(p['weight'] for p in parts)
    vector = [sum(p['vector'][i] * p['weight'] for p in parts) / total
              for i in range(len(parts[0]['vector']))]
    norm = math.sqrt(sum(x*x for x in vector)) or 1
    return pack([x/norm for x in vector])


def make_embedding(entries, vectors):
    parts = [{'text': e['text'], 'embedding': base64.b64encode(pack(v)).decode('ascii')}
             for e, v in zip(entries, vectors)]
    aggregate = combine([{'weight': e['weight'], 'vector': v} for e, v in zip(entries, vectors)])
    return ProfileEmbedding(aggregate, parts)


def parts_json(vector):
    return getattr(vector, 'parts_json', '[]')


def with_parts(profile):
    if profile['embedding'] is None:
        return None
    return ProfileEmbedding(profile['embedding'], json.loads(profile.get('embedding_parts') or '[]'))


def active_vectors(profile):
    if not profile:
        return []
    from .profile import embedding_inputs
    stored = json.loads(profile.get('embedding_parts') or '[]')
    if stored:
        weights = dict(embedding_inputs(profile['content']))
        result = {}
        for part in stored:
            weight = weights.get(part['text'], 0)
            if weight <= 0:
                continue
            blob = base64.b64decode(part['embedding'])
            vector = unpack(blob)
            result[part['text']] = {'key': part['text'], 'weight': weight, 'blob': blob, 'vector': vector,
                                    'norm': math.sqrt(math.sumprod(vector,vector))}
        return list(result.values())
    blob = profile.get('embedding')
    if not blob:return []
    vector=unpack(blob)
    return [{'key': '', 'weight': 1., 'blob': blob, 'vector': vector,
             'norm':math.sqrt(math.sumprod(vector,vector))}]


def best_match(vectors, paper):
    if not vectors or not paper:
        return None, 0.
    paper_norm=math.sqrt(math.sumprod(paper,paper))
    def similarity(part):
        vector=part['vector']
        if len(vector)!=len(paper):return 0.
        norm=part['norm']*paper_norm
        return math.sumprod(vector,paper)/norm if norm else 0.
    return max(((part,similarity(part)) for part in vectors),key=lambda item:(item[1],item[0]['weight']))


def quotas(weights, budget):
    """Largest-remainder allocation: all directions share one exact budget."""
    total = sum(weights)
    if not total:
        return [0] * len(weights)
    exact = [budget*w/total for w in weights]
    counts = [math.floor(value) for value in exact]
    for index in sorted(range(len(weights)), key=lambda i: exact[i]-counts[i], reverse=True)[:budget-sum(counts)]:
        counts[index] += 1
    return counts
