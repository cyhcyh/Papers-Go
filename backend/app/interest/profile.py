from .. import prompts
from ..llm import runtime as models
import json
import re
from datetime import date
from ..config import now, today
from ..db import connect, one, rows, dumps, validate_vectors
from ..llm.ollama import ollama
from ..llm.provider import cloud
from .form import ProfileForm, parse_form, render_form, section_kind, normalize_recent_name
from .vectors import make_embedding, parts_json

CURRENT_PROFILE_IDS = 'SELECT MAX(id) FROM interest_profile GROUP BY user_id'


def current(user_id):
    return one('SELECT * FROM interest_profile WHERE user_id=? ORDER BY version DESC LIMIT 1', (user_id,))


def active_entries(content, on_date=None, *, include_inferred=True):
    entries = []
    section = 'long_term'
    for line in content.splitlines():
        if line.startswith('##'):
            section = section_kind(line)
        if section == 'description' or section == 'inferred' and not include_inferred:
            continue
        if not line.strip().startswith('- '):
            continue
        ttl = re.search(r'until:(\d{4}-\d{2}-\d{2})', line)
        if ttl and ttl[1] < (on_date or today()):
            continue
        weight = re.search(r'w:([\d.]+)', line)
        text = re.sub(r'\[w:[^]]*\]', '', line[2:]).strip()
        entries.append({'text': text, 'weight': min(1, float(weight[1])) if weight else .7, 'excluded': section == 'exclusions'})
    return entries


def embedding_inputs(content, on_date=None):
    weights={}
    for entry in active_entries(content,on_date):
        if not entry['excluded'] and entry['weight']>0:
            weights[entry['text']]=max(weights.get(entry['text'],0),entry['weight'])
    return list(weights.items())


def put_profile(user_id, content, structured, reason, embedding=None, db=None):
    content = normalize_recent_name(content)
    def write(conn):
        vector=embedding
        saved=conn.execute("SELECT value FROM app_settings WHERE name='models'").fetchone()
        if saved and vector is not None:
            from ..llm.secrets import decrypt_configuration
            live=decrypt_configuration(json.loads(saved['value']))
            if models.embedding_identity(live)!=models.embedding_identity(models.configuration()) or len(vector)!=live['embedding_dim']*4:
                vector=None  # A model switch finished while this old request was generating.
        previous = conn.execute('SELECT version,content,embedding,embedding_parts FROM interest_profile WHERE user_id=? ORDER BY version DESC LIMIT 1', (user_id,)).fetchone()
        version = previous['version'] + 1 if previous else 1
        parts = getattr(vector, 'parts_json', None) if vector is not None else ('[]' if not embedding_inputs(content) else None)
        # Topic/source maintenance copies the profile without changing preferences.
        if vector is not None and parts is None and previous and vector==previous['embedding'] and embedding_inputs(content)==embedding_inputs(previous['content']):
            parts=previous['embedding_parts']
        profile_id = conn.execute('INSERT INTO interest_profile(user_id,version,content,structured,embedding,embedding_parts,change_reason,created_at) VALUES(?,?,?,?,?,?,?,?)',
                         (user_id, version, content, dumps(structured), vector, parts, reason, now())).lastrowid
        conn.execute('INSERT INTO audit_log(user_id,action,detail,created_at) VALUES(?,?,?,?)',
                     (user_id, 'profile.' + reason, dumps({'version': version}), now()))
        trim_history(conn, user_id)
        return {'id': profile_id, 'version': version}
    if db is not None:
        return write(db)
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        return write(conn)


def trim_history(db, user_id):
    # The current profile plus at most three older versions; version numbers never reset.
    db.execute('DELETE FROM interest_profile WHERE user_id=? AND id NOT IN (SELECT id FROM interest_profile WHERE user_id=? ORDER BY version DESC LIMIT 4)', (user_id, user_id))


def migrate_background(db):
    """Invalidate legacy current vectors that included research background."""
    name = 'interest-background-v1'
    if db.execute('SELECT 1 FROM app_migrations WHERE name=?', (name,)).fetchone():
        return
    profiles = db.execute(f'SELECT id,content FROM interest_profile WHERE id IN ({CURRENT_PROFILE_IDS})').fetchall()
    for profile in profiles:
        content = normalize_recent_name(profile['content'])
        if content != profile['content']:
            db.execute('UPDATE interest_profile SET content=? WHERE id=?', (content, profile['id']))
            db.execute('UPDATE embedding_rebuild_profiles SET content=? WHERE profile_id=? AND content=?',
                       (content, profile['id'], profile['content']))
        if not parse_form(profile['content'])['description']:
            continue
        db.execute('UPDATE interest_profile SET embedding=NULL,embedding_parts=NULL WHERE id=?', (profile['id'],))
        db.execute('DELETE FROM embedding_rebuild_profiles WHERE profile_id=?', (profile['id'],))
    # Existing overviews may also have used inferred interests as directions.
    db.execute("DELETE FROM direction_trends WHERE audience LIKE 'user:%'")
    db.execute('INSERT INTO app_migrations(name,applied_at) VALUES(?,?)', (name, now()))


async def profile_embedding(content, *, strict=False):
    entries = [{'text':text,'weight':weight} for text,weight in embedding_inputs(content)]
    try:
        if not entries:
            return None
        from .retrieval_query import search_queries, prompt_revision
        from ..vector_store import epoch
        revision, vector_epoch = prompt_revision(), epoch()
        queries = await search_queries(content, entries, strict=strict)
        vectors = await models.embed(queries)
        validate_vectors(vectors, len(entries))
        result = make_embedding(entries, vectors)
        parts = json.loads(result.parts_json)
        for part, query in zip(parts, queries):
            part.update(query=query, query_revision=revision, vector_epoch=vector_epoch)
        result.parts_json = json.dumps(parts, ensure_ascii=False, separators=(',', ':'))
        return result
    except Exception:
        if strict:
            raise
        return None


def save_current_embedding(profile, vector):
    """Discard a result superseded by a profile edit or a completed model switch."""
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        saved = db.execute("SELECT value FROM app_settings WHERE name='models'").fetchone()
        if saved:
            from ..llm.secrets import decrypt_configuration
            live = decrypt_configuration(json.loads(saved['value']))
            if models.embedding_identity(live) != models.embedding_identity(models.configuration()):
                return False
        return bool(db.execute(f'UPDATE interest_profile SET embedding=?,embedding_parts=? WHERE id=? AND content=? AND id IN ({CURRENT_PROFILE_IDS})',
            (vector, parts_json(vector), profile['id'], profile['content'])).rowcount)


@models.model_task
async def initialize(user_id, description, topic_ids, exclusions, selection=None):
    selected = [t for t in rows("SELECT * FROM topics WHERE status='active'") if t['id'] in topic_ids]
    valid_ids = [t['id'] for t in selected]
    content = render_form(ProfileForm(description=description, exclusions=exclusions))
    selected_entries = '\n'.join(f"- [w:0.7] {t['name_zh']}（{t['name_en']}）" for t in selected)
    content = content.replace('## 核心兴趣（长期）', '## 核心兴趣（长期）\n' + selected_entries, 1)
    structured = {'topic_ids': valid_ids}
    if selection is not None:
        structured['category_selection'] = selection
    return put_profile(user_id, content, structured, 'init', await profile_embedding(content))


def cloud_key():
    from ..config import settings
    return settings().llm_api_key
