"""Versioned bundled reference data, with persistent per-field local overrides."""
import json
from pathlib import Path

from .config import now

# Increment this whenever resources/research_areas.json changes.
CATALOG_VERSION = 2
VERSION_SETTING = 'research_catalog_version'
RESOURCE_DIR = Path(__file__).parent / 'resources'
EDITABLE = ('discipline', 'name', 'description')


def bundled_catalog(filename='research_areas.json'):
    from .standard_topics import validate_area, normalized_name
    areas = [validate_area(a) for a in json.loads((RESOURCE_DIR / filename).read_text(encoding='utf-8'))]
    if len({a['id'] for a in areas}) != len(areas) or len({normalized_name(a['name']) for a in areas}) != len(areas):
        raise ValueError('研究方向目录存在重复')
    return {a['id']: a for a in areas}


def initialize_state(db):
    db.execute('''CREATE TABLE IF NOT EXISTS research_area_state(
        id TEXT PRIMARY KEY REFERENCES research_areas(id) ON DELETE CASCADE,
        origin TEXT NOT NULL,base_json TEXT,overrides_json TEXT NOT NULL DEFAULT '[]')''')


def record_edit(db, area):
    """Approval of an unchanged entry is not a local override."""
    row = db.execute('SELECT * FROM research_area_state WHERE id=?', (area['id'],)).fetchone()
    if row is None:
        db.execute("INSERT INTO research_area_state(id,origin) VALUES(?,'local')", (area['id'],))
    elif row['origin'] == 'builtin':
        baseline = json.loads(row['base_json'])
        changed = [field for field in EDITABLE if area[field] != baseline[field]]
        db.execute('UPDATE research_area_state SET overrides_json=? WHERE id=?',
                   (json.dumps(changed), area['id']))


def allocate_local_id(db):
    counter = 'research_area_local_last'
    saved = db.execute('SELECT value FROM app_settings WHERE name=?', (counter,)).fetchone()
    maximum = max([int(r[0].rsplit('-', 1)[1]) for r in
                   db.execute("SELECT id FROM research_areas WHERE id LIKE 'RA-LOCAL-%'")]
                  + [int(saved[0]) if saved else 0])
    db.execute('INSERT INTO app_settings(name,value,updated_at) VALUES(?,?,?) '
               'ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
               (counter, str(maximum + 1), now()))
    return f'RA-LOCAL-{maximum + 1:06}'


def move_local_entry(db, area):
    """Only old custom IDs colliding with a new bundled ID need relocation."""
    from .standard_topics import entry
    old = area['id']
    relocated = {**area, 'id': allocate_local_id(db)}
    key = relocated['id']
    db.execute('INSERT INTO research_areas(id,discipline,name,description) VALUES(?,?,?,?)',
               tuple(relocated[k] for k in ('id', *EDITABLE)))
    db.execute("INSERT INTO research_area_state(id,origin) VALUES(?,'local')", (key,))
    db.execute('UPDATE topics SET standard_key=?,standard_code=?,standard_path=? WHERE standard_key=?',
               (key, key, entry(relocated)['path'], old))
    db.execute('UPDATE topic_proposals SET standard_key=? WHERE standard_key=?', (key, old))
    db.execute('UPDATE watches SET topic_key=? WHERE topic_key=?', (key, old))
    db.execute('UPDATE paper_classifications SET standard_key=? WHERE standard_key=?', (key, old))
    for row in db.execute('SELECT paper_id,previous_result FROM paper_classifications '
                          'WHERE previous_result LIKE ?', ('%' + old + '%',)).fetchall():
        value = json.loads(row['previous_result'])
        def replace(item):
            if isinstance(item, dict):
                return {k: key if k == 'standard_key' and v == old else replace(v) for k, v in item.items()}
            if isinstance(item, list):
                return [replace(v) for v in item]
            return item
        db.execute('UPDATE paper_classifications SET previous_result=? WHERE paper_id=?',
                   (json.dumps(replace(value), ensure_ascii=False), row['paper_id']))
    db.execute('DELETE FROM research_areas WHERE id=?', (old,))


def update_attached_topics(db, previous, area):
    from .standard_topics import entry, ZH
    key = area['id']
    # Preserve topic-specific translations and edits that differ from the catalog.
    for field, column in (('name', 'name_en'), ('description', 'description'), ('discipline', 'discipline')):
        if previous[field] != area[field]:
            db.execute(f'UPDATE topics SET {column}=? WHERE standard_key=? AND {column}=?',
                       (area[field], key, previous[field]))
    if previous['name'] != area['name']:
        db.execute('UPDATE topics SET name_zh=? WHERE standard_key=? AND name_zh=?',
                   (ZH.get(area['name'], area['name']), key, ZH.get(previous['name'], previous['name'])))
    db.execute('UPDATE topics SET standard_system=?,standard_path=? WHERE standard_key=?',
               (area['discipline'], entry(area)['path'], key))


def synchronize(db):
    """Call during initialization; version and data commit in the same transaction."""
    from .standard_topics import bump
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    initialize_state(db)
    saved = db.execute('SELECT value FROM app_settings WHERE name=?', (VERSION_SETTING,)).fetchone()
    # Restart or rollback to an older image must never downgrade persisted data.
    if saved and int(saved['value']) >= CATALOG_VERSION:
        return {'changed': 0, 'version': int(saved['value'])}
    db.execute('SAVEPOINT research_catalog_sync')
    try:
        current = bundled_catalog()
        legacy = bundled_catalog('research_areas_v1.json') if saved is None else {}
        installed = {r['id']: dict(r) for r in db.execute('SELECT * FROM research_areas')}
        states = {r['id']: dict(r) for r in db.execute('SELECT * FROM research_area_state')}
        # Adopt old catalogs once. Known old baseline differences are local edits;
        # unknown records stay local instead of being guessed or overwritten.
        for key, area in installed.items():
            if key in states:
                continue
            baseline = legacy.get(key)
            if baseline is None and area == current.get(key):
                baseline = current[key]
            state = {'id': key, 'origin': 'builtin' if baseline else 'local',
                     'base_json': json.dumps(baseline, ensure_ascii=False) if baseline else None,
                     'overrides_json': json.dumps([f for f in EDITABLE if area[f] != baseline[f]]) if baseline else '[]'}
            db.execute('INSERT INTO research_area_state VALUES(:id,:origin,:base_json,:overrides_json)', state)
            states[key] = state
        changed = 0
        for key, baseline in current.items():
            previous = installed.get(key)
            state = states.get(key)
            if previous and state['origin'] != 'builtin':
                move_local_entry(db, previous)
                previous = None
                changed += 1
            if previous is None:
                area = baseline
                db.execute('INSERT INTO research_areas(id,discipline,name,description) VALUES(?,?,?,?)',
                           tuple(area[k] for k in ('id', *EDITABLE)))
                changed += 1
            else:
                old_base = json.loads(state['base_json'])
                # The comparison also preserves edits made by older application versions.
                overridden = set(json.loads(state['overrides_json'])) | {f for f in EDITABLE if previous[f] != old_base[f]}
                area = {**baseline, **{f: previous[f] for f in overridden}}
                if area != previous:
                    db.execute('UPDATE research_areas SET discipline=?,name=?,description=? WHERE id=?',
                               (*[area[f] for f in EDITABLE], key))
                    update_attached_topics(db, previous, area)
                    changed += 1
            overrides = [f for f in EDITABLE if area[f] != baseline[f]]
            db.execute("INSERT INTO research_area_state VALUES(?,'builtin',?,?) ON CONFLICT(id) DO UPDATE SET "
                       "origin='builtin',base_json=excluded.base_json,overrides_json=excluded.overrides_json",
                       (key, json.dumps(baseline, ensure_ascii=False), json.dumps(overrides)))
        if changed:
            bump(db)
        db.execute('INSERT INTO app_settings(name,value,updated_at) VALUES(?,?,?) '
                   'ON CONFLICT(name) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at',
                   (VERSION_SETTING, str(CATALOG_VERSION), now()))
        db.execute('RELEASE research_catalog_sync')
        return {'changed': changed, 'version': CATALOG_VERSION}
    except BaseException:
        db.execute('ROLLBACK TO research_catalog_sync')
        db.execute('RELEASE research_catalog_sync')
        raise
