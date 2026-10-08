import json
import sqlite3
from datetime import date, timedelta

import pytest

from app.config import today
from app.db import connect, execute, one, rows
from app import research_catalog as upgrades
from app.standard_topics import catalog, save_area, revision
from .conftest import headers


def next_release(monkeypatch, changes):
    baseline = upgrades.bundled_catalog()
    for key, fields in changes.items():
        baseline[key] = {**baseline.get(key, {'id': key, 'discipline': 'Physics'}), **fields}
    monkeypatch.setattr(upgrades, 'CATALOG_VERSION', upgrades.CATALOG_VERSION + 1)
    monkeypatch.setattr(upgrades, 'bundled_catalog', lambda filename='research_areas.json': baseline)


def test_upgrade_merges_by_field_and_keeps_papers_and_topics(client, accounts, papers, monkeypatch):
    key = 'RA-CS-001'
    base = catalog()[key]
    with connect() as db:
        save_area(db, key, base['discipline'], base['name'], 'Administrator description')
    before = rows('SELECT id,classified,classification_state FROM papers ORDER BY id')
    links = rows('SELECT * FROM paper_topics ORDER BY paper_id')
    next_release(monkeypatch, {
        key: {'name': 'Revised Algorithmic Game Theory', 'description': 'New bundled description'},
        'RA-PHYS-999': {'name': 'New Physics Research', 'description': 'New reference research direction.'}})
    with connect() as db:
        result = upgrades.synchronize(db)
    assert result['changed'] == 2
    assert catalog()[key]['name'] == 'Revised Algorithmic Game Theory'
    assert catalog()[key]['description'] == 'Administrator description'
    state = one('SELECT * FROM research_area_state WHERE id=?', (key,))
    assert json.loads(state['base_json'])['description'] == 'New bundled description'
    assert json.loads(state['overrides_json']) == ['description']
    assert rows('SELECT id,classified,classification_state FROM papers ORDER BY id') == before
    assert rows('SELECT * FROM paper_topics ORDER BY paper_id') == links


def test_unchanged_approval_does_not_mark_override(client, accounts, monkeypatch):
    base = catalog()['RA-CS-001']
    response = client.post('/api/admin/topics', headers=headers(accounts[0]), json={
        'name_zh': '算法博弈论', 'name_en': base['name'], 'description': base['description'],
        'discipline': base['discipline'], 'standard_key': base['id'], 'category_keys': ['arxiv:cs.AI']})
    assert response.status_code == 200, response.text
    tid = response.json()['id']
    assert one('SELECT overrides_json FROM research_area_state WHERE id=?', (base['id'],))['overrides_json'] == '[]'
    next_release(monkeypatch, {base['id']: {'description': 'A revised research direction description.'}})
    with connect() as db:
        upgrades.synchronize(db)
    topic = one('SELECT * FROM topics WHERE id=?', (tid,))
    assert topic['name_zh'] == '算法博弈论'
    assert topic['description'] == 'A revised research direction description.'


def test_restart_and_older_image_skip_file_reads_and_writes(client, monkeypatch):
    def fail(*args):
        raise AssertionError('Unchanged version must not reread the catalog')
    monkeypatch.setattr(upgrades, 'bundled_catalog', fail)
    with connect() as db:
        before = revision(db)
        assert upgrades.synchronize(db)['changed'] == 0
        monkeypatch.setattr(upgrades, 'CATALOG_VERSION', 1)
        assert upgrades.synchronize(db)['changed'] == 0
        assert revision(db) == before


def test_sync_failure_rolls_back_records_and_version(client, monkeypatch):
    before = rows('SELECT * FROM research_areas ORDER BY id')
    old_version = one('SELECT value FROM app_settings WHERE name=?', (upgrades.VERSION_SETTING,))['value']
    next_release(monkeypatch, {'RA-CS-001': {'description': 'New bundled description'},
                              'RA-PHYS-999': {'name': 'New Physics Research', 'description': 'A new direction.'}})
    with connect() as db:
        db.execute("CREATE TEMP TRIGGER fail_sync BEFORE INSERT ON research_areas WHEN NEW.id='RA-PHYS-999' BEGIN SELECT RAISE(ABORT,'simulated interruption'); END")
        with pytest.raises(sqlite3.IntegrityError, match='simulated interruption'):
            upgrades.synchronize(db)
    assert rows('SELECT * FROM research_areas ORDER BY id') == before
    assert one('SELECT value FROM app_settings WHERE name=?', (upgrades.VERSION_SETTING,))['value'] == old_version


def test_old_catalog_adoption_preserves_overrides_and_local_records(client, monkeypatch):
    with connect() as db:
        db.execute('DELETE FROM app_settings WHERE name=?', (upgrades.VERSION_SETTING,))
        db.execute('DELETE FROM research_area_state')
        legacy = upgrades.bundled_catalog('research_areas_v1.json')
        db.execute('DELETE FROM research_areas')
        db.executemany('INSERT INTO research_areas VALUES(:id,:discipline,:name,:description)', list(legacy.values()))
        db.execute("UPDATE research_areas SET description='Existing administrator description' WHERE id='RA-CS-001'")
        db.execute("INSERT INTO research_areas VALUES('RA-CS-078','Computer Science','Existing Local Direction','Local description')")
        assert upgrades.synchronize(db)['changed'] == 226
    assert len(catalog()) == 389
    assert catalog()['RA-CS-001']['description'] == 'Existing administrator description'
    assert catalog()['RA-CS-078']['name'] == 'Existing Local Direction'
    assert one("SELECT origin FROM research_area_state WHERE id='RA-CS-078'")['origin'] == 'local'


def test_new_builtin_id_collision_preserves_local_topic_identity(client, accounts, papers, monkeypatch):
    with connect() as db:
        area = save_area(db, None, 'Physics', 'Local Physics Direction', 'Local description')
        # Simulate a custom ID allocated by an older release.
        legacy_key = 'RA-PHYS-999'
        db.execute('INSERT INTO research_areas VALUES(?,?,?,?)', (legacy_key, area['discipline'], area['name'], area['description']))
        db.execute("INSERT INTO research_area_state(id,origin) VALUES(?,'local')", (legacy_key,))
        db.execute('DELETE FROM research_areas WHERE id=?', (area['id'],))
        db.execute('UPDATE topics SET standard_key=?,standard_code=? WHERE id=15', (legacy_key, legacy_key))
    links = rows('SELECT * FROM paper_topics ORDER BY paper_id')
    next_release(monkeypatch, {legacy_key: {'name': 'New Physics Research', 'description': 'New bundled direction.'}})
    with connect() as db:
        upgrades.synchronize(db)
    topic = one('SELECT standard_key FROM topics WHERE id=15')
    assert topic['standard_key'].startswith('RA-LOCAL-')
    assert catalog()[topic['standard_key']]['name'] == 'Local Physics Direction'
    assert catalog()[legacy_key]['name'] == 'New Physics Research'
    assert rows('SELECT * FROM paper_topics ORDER BY paper_id') == links


def test_two_calendar_days_respects_selected_date_basis(client, papers, monkeypatch):
    from app.api import content
    fixed = '2026-10-08'
    monkeypatch.setattr(content, 'today', lambda: fixed)
    for ident, published, ingested in zip(papers, ['2026-10-08', '2026-10-07', '2026-10-06'],
                                        ['2026-10-06', '2026-10-07', '2026-10-08']):
        execute('UPDATE papers SET published=?,ingested_date=? WHERE id=?', (published, ingested, ident))
    publication = client.get('/api/browse?range=two_days&date_basis=paper').json()
    collection = client.get('/api/browse?range=two_days&date_basis=ingested').json()
    assert {p['id'] for p in publication['items']} == set(papers[:2])
    assert {p['id'] for p in collection['items']} == set(papers[1:])
    assert client.get('/api/browse').json()['total'] == 3
