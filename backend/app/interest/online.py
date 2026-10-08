import math
import base64
import json
from ..db import unpack, pack
from .vectors import active_vectors, best_match, combine


def update_vector(profile_blob, paper_blob, action):
    if not paper_blob or action not in ('like', 'skip'):
        return profile_blob
    paper = unpack(paper_blob)
    current = unpack(profile_blob)
    if not current:
        return paper_blob if action != 'skip' else None
    alpha = .1
    direction = -1 if action == 'skip' else 1
    updated = [(1-alpha)*a + direction*alpha*b for a, b in zip(current, paper)]
    norm = math.sqrt(sum(x*x for x in updated)) or 1
    return pack([x/norm for x in updated])


def update_profile(profile, paper_blob, action):
    blob, stored = profile['embedding'], profile.get('embedding_parts')
    if not paper_blob or action not in ('like', 'skip'):
        return blob, stored, None
    parts = json.loads(stored or '[]')
    if not parts:
        return update_vector(blob, paper_blob, action), stored, None
    active = active_vectors(profile)
    matched, _ = best_match(active, unpack(paper_blob))
    if matched is None or len(matched['blob']) != len(paper_blob):
        return blob, stored, None
    changed = update_vector(matched['blob'], paper_blob, action)
    before = base64.b64encode(matched['blob']).decode('ascii')
    after = base64.b64encode(changed).decode('ascii')
    for part in parts:
        if part['text'] == matched['key']:
            part['embedding'] = after
    matched['blob'], matched['vector'] = changed, unpack(changed)
    return combine(active), json.dumps(parts, ensure_ascii=False, separators=(',', ':')), {
        'key': matched['key'], 'before': before, 'after': after}


def restore_part(stored, delta):
    parts = json.loads(stored or '[]')
    matched = [p for p in parts if p['text'] == delta['key']]
    # A same-model redo may have completed after this feedback was recorded.
    if not matched or any(p['embedding'] != delta['after'] for p in matched):
        return None
    for part in matched:
        part['embedding'] = delta['before']
    return json.dumps(parts, ensure_ascii=False, separators=(',', ':'))
