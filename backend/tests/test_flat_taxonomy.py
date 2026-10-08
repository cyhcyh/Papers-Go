import json
import pytest
from app.config import now
from app.db import connect, execute, one, rows, dumps
from app.interest.profile import current, put_profile
from .legacy_catalog import migrate_flat_topics


def test_directory_is_two_levels_and_observed_tags_cannot_promote_topics(client, papers):
    execute('UPDATE papers SET primary_category=?,categories=? WHERE id=?', ('math.CO', '[]', papers[1]))
    execute('INSERT INTO paper_topics VALUES(?,?,.99)', (papers[1], 2))
    catalog = {c['key']: c for c in client.get('/api/categories').json()}
    assert len(catalog) == 13
    assert all(t['parent_id'] is None for c in catalog.values() for t in c['topics'])
    math_names = {t['name_en'] for t in catalog['arxiv:math.CO']['topics']}
    assert {'Graph coloring and Ramsey theory', 'Extremal combinatorics', 'Enumerative combinatorics'} <= math_names
    assert not {'Artificial intelligence', 'Agents', 'Machine learning', 'Language models', 'Combinatorics'} & math_names
    assert 3 in {t['id'] for t in catalog['arxiv:cs.AI']['topics']}
    assert 3 not in {t['id'] for t in catalog['arxiv:math.CO']['topics']}
    assert {t['id']: t['paper_count'] for t in catalog['arxiv:math.CO']['topics']}[15] == 1


def test_old_taxonomy_migration_repairs_tags_and_preserves_papers_and_profile_history(client, accounts, papers):
    execute("DELETE FROM app_migrations WHERE name='flat_source_topics_v1'")
    execute("UPDATE topics SET status='active',category_keys='[]' WHERE id=1")
    execute('UPDATE topics SET parent_id=1 WHERE id=2')
    execute('UPDATE topics SET parent_id=2 WHERE id=3')
    execute('UPDATE topics SET parent_id=14 WHERE id=15')
    execute('UPDATE topics SET category_keys=? WHERE id=2', (dumps(['arxiv:math.CO']),))
    custom = execute('INSERT INTO topics(name_zh,name_en,parent_id,created_at) VALUES(?,?,?,?)', ('自定义智能体主题','Custom agent topic',2,now()))
    execute('UPDATE papers SET primary_category=?,categories=?,classified=1 WHERE id=?', ('math.CO','[]',papers[1]))
    execute('INSERT INTO paper_topics VALUES(?,?,.99)', (papers[1],1))
    execute('INSERT INTO paper_topics VALUES(?,?,.99)', (papers[1],2))
    execute('UPDATE papers SET classified=1 WHERE id=?', (papers[0],))
    uid = accounts[0]['user']['id']
    profile = put_profile(uid, '保留原有研究描述', {'topic_ids':[1]}, 'init')
    execute('INSERT INTO user_paper_state(user_id,paper_id,saved,updated_at) VALUES(?,?,1,?)', (uid,papers[1],now()))
    before = rows('SELECT id,title,abstract FROM papers ORDER BY id')
    with connect() as db:
        migrate_flat_topics(db)
    assert rows('SELECT id,title,abstract FROM papers ORDER BY id') == before
    assert one('SELECT saved FROM user_paper_state WHERE user_id=? AND paper_id=?',(uid,papers[1]))['saved'] == 1
    assert one('SELECT parent_id FROM topics WHERE id=?',(custom,))['parent_id'] is None
    assert set(json.loads(one('SELECT category_keys FROM topics WHERE id=?',(custom,))['category_keys'])) >= {'arxiv:cs.AI','arxiv:cs.MA'}
    assert one('SELECT * FROM paper_topics WHERE paper_id=? AND topic_id=1',(papers[1],)) is None
    assert one('SELECT * FROM paper_topics WHERE paper_id=? AND topic_id=2',(papers[1],)) is None
    assert one('SELECT * FROM paper_topics WHERE paper_id=? AND topic_id=15',(papers[1],))
    assert one('SELECT classified FROM papers WHERE id=?',(papers[1],))['classified'] == 0
    assert one('SELECT classified FROM papers WHERE id=?',(papers[0],))['classified'] == 1
    latest = current(uid)
    assert latest['version'] == profile['version'] + 1
    assert latest['content'] == '保留原有研究描述'
    assert 'arxiv:cs.AI' in json.loads(latest['structured'])['category_selection']['categories']
    execute("UPDATE topics SET name_en='Renamed agents' WHERE id=2")
    with connect() as db:
        migrate_flat_topics(db)
    assert current(uid)['version'] == latest['version']
    assert one('SELECT name_en FROM topics WHERE id=2')['name_en'] == 'Renamed agents'


@pytest.mark.asyncio
async def test_classifier_rejects_high_confidence_topic_from_an_unrelated_source(client, papers, monkeypatch):
    from app.pipeline.classify import classify_paper
    from app.llm import runtime
    execute('UPDATE papers SET primary_category=?,categories=? WHERE id=?', ('math.CO','[]',papers[1]))
    async def complete(feature,messages,**kwargs):
        prompt=messages[0]['content']
        options=json.loads(prompt.rsplit('可选研究方向：',1)[1].split('\n论文标题：')[0])
        assert all(t['id'].startswith('RA-') for t in options)
        assert 'RA-MATH-060' in {t['id'] for t in options}
        return {'standard_key':'CCS2012:invented','confidence':.99,'name_zh':'无关主题','reason':'无关','evidence':'Graph','no_suitable_topic':False}
    monkeypatch.setattr(runtime,'complete',complete)
    with pytest.raises(ValueError,match='候选目录'):
        await classify_paper(one('SELECT * FROM papers WHERE id=?',(papers[1],)))
    assert not one("SELECT id FROM topics WHERE name_zh='无关主题'")


@pytest.mark.asyncio
async def test_audit_repairs_only_with_source_scoped_topics(client, papers, monkeypatch):
    from app.pipeline.metrics import audit
    from app.llm.provider import cloud
    execute('UPDATE papers SET primary_category=?,categories=? WHERE id=?', ('math.CO','[]',papers[1]))
    execute('DELETE FROM paper_topics WHERE paper_id=?',(papers[1],))
    execute('INSERT INTO paper_topics VALUES(?,?,1)',(papers[1],16))
    async def complete(*args, **kwargs):
        return {'checks':[{'id':papers[1],'correct':False,'suggested_topic_ids':[3,15]}]}
    monkeypatch.setattr(cloud,'complete',complete)
    await audit()
    assert [t['topic_id'] for t in rows('SELECT topic_id FROM paper_topics WHERE paper_id=?',(papers[1],))] == [15]
