from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi import HTTPException

from app import agent_skills as skills, retention, usage_history
from app.api.site import _compute_overview
from app.config import now, today, settings
from app.db import connect, execute, one, rows
from .conftest import headers
from .test_agent_skills import payload


def test_skill_history_and_files_preserve_lazy_in_use_version(client, accounts):
    user=accounts[1];uid=user['user']['id']
    item=skills.create(skills.SkillEdit(**payload()),uid)
    sid=client.post('/api/chat/sessions',headers=headers(user),json={}).json()['id']
    initial=one('SELECT version_id FROM agent_skills WHERE id=?',(item['id'],))['version_id']
    with skills.invocation():
        original=skills.activate(item['id'],uid,sid)
        for n in range(2,10):
            skills.save(item['id'],skills.SkillEdit(**payload(text='Version '+str(n),resources={'references/checklist.md':'new'})),uid)
        assert retention.skill_versions(now())==2
        retention.skill_files(now())
        versions=rows('SELECT revision FROM agent_skill_versions WHERE skill_id=? ORDER BY revision',(item['id'],))
        assert [v['revision'] for v in versions]==[1,4,5,6,7,8,9]
        assert skills.activation_content(skills.record(item['id']))['instructions']==original['instructions']
        assert skills.resource(item['id'],'references/checklist.md',uid,sid)['text']=='问题、方法、实验和限制。'
        assert (skills.root()/item['id']/initial).is_dir()
    assert retention.skill_versions(now())==1
    retention.skill_files(now())
    assert not (skills.root()/item['id']/initial).exists()
    assert len(list((skills.root()/item['id']).iterdir()))==6
    assert skills.detail(item['id'],uid)['text']=='Version 9'
    restored=skills.restore(item['id'],4,uid)
    assert restored['revision']==10 and restored['text']=='Version 4'


def test_failed_skill_file_cleanup_is_retried_without_affecting_current(client,accounts,monkeypatch):
    uid=accounts[1]['user']['id'];item=skills.create(skills.SkillEdit(**payload()),uid)
    for _ in range(7):skills.save(item['id'],skills.SkillEdit(**payload()),uid)
    retention.skill_versions(now())
    queued=one('SELECT * FROM skill_bundle_cleanup LIMIT 1')
    original=skills.remove_version_files
    def denied(*args):raise PermissionError('busy')
    monkeypatch.setattr(skills,'remove_version_files',denied)
    retention.skill_files(now())
    assert one('SELECT 1 FROM skill_bundle_cleanup WHERE version_id=?',(queued['version_id'],))
    assert skills.detail(item['id'],uid)['text']==payload()['text']
    monkeypatch.setattr(skills,'remove_version_files',original)
    retention.skill_files((datetime.fromisoformat(now())+timedelta(hours=2)).isoformat())
    assert not one('SELECT 1 FROM skill_bundle_cleanup')
    with pytest.raises(HTTPException):
        skills.remove_version_files(item['id'],'../outside')


def test_usage_cap_archives_before_delete_and_preserves_overview(client,accounts,monkeypatch):
    local=datetime.fromisoformat(today()).replace(tzinfo=ZoneInfo(settings().tz))
    stamps=[(local+timedelta(minutes=n)).astimezone(timezone.utc).isoformat() for n in range(1,7)]
    with connect() as db:
        db.executemany('''INSERT INTO llm_usage(model,feature,connection_id,billing_source,input_tokens,output_tokens,usage_known,created_at)
            VALUES(?,?,?,?,?,?,?,?)''',[
            ('model','classify','cloud','api',10,3,1,stamps[0]),
            ('model','classify','cloud','api',0,0,0,stamps[1]),
            ('codex','chat','codex','subscription',20,4,1,stamps[2]),
            ('legacy',None,None,'api',5,2,1,stamps[3]),
            ('model','chat','cloud','api',8,1,1,stamps[4]),
            ('codex','chat','codex','subscription',0,0,0,stamps[5])])
    before=_compute_overview(7);before.pop('updated_at')
    monkeypatch.setattr(retention,'USAGE_LIMIT',2)
    assert retention.usage(now())==4
    assert one('SELECT COUNT(*) n FROM llm_usage')['n']==2
    assert one('SELECT SUM(calls) n FROM llm_usage_daily')['n']==4
    after=_compute_overview(7);after.pop('updated_at')
    assert after==before
    assert retention.usage(now())==0  # Never add the same usage twice.
    with connect() as db:
        legacy=usage_history.usage_rows(db,stamps[0][:10])
    assert sum(r['calls'] for r in legacy)==6


def test_usage_archive_failure_rolls_back_summary_and_raw_rows(client,monkeypatch):
    execute('INSERT INTO llm_usage(model,input_tokens,output_tokens,created_at) VALUES(?,?,?,?)',
            ('old',100,20,'2020-01-01T00:00:00+00:00'))
    original=usage_history.archive
    def fail(db,records):
        original(db,records)
        raise ValueError('interrupted aggregation')
    monkeypatch.setattr(usage_history,'archive',fail)
    with pytest.raises(ValueError):retention.usage(now())
    assert one('SELECT COUNT(*) n FROM llm_usage')['n']==1
    assert not one('SELECT 1 FROM llm_usage_daily')


def test_redo_cleanup_is_bounded_and_protects_latest_and_queued_runs(client,papers,monkeypatch):
    stamp=now();ids=[]
    with connect() as db:
        for n in range(7):
            ids.append(db.execute('INSERT INTO pipeline_redo_runs(name,options,status,created_at,updated_at) VALUES(?,?,?,?,?)',
                ('classify','{}','stopped' if n<6 else 'running',stamp,stamp)).lastrowid)
        for pid in papers:
            db.execute("INSERT INTO pipeline_redo_items(run_id,paper_id,component) VALUES(?,?,'classification')",(ids[0],pid))
        db.execute("INSERT INTO pipeline_commands(action,name,status,created_at,redo_id) VALUES('start','classify','queued',?,?)",(stamp,ids[1]))
    monkeypatch.setattr(retention,'BATCH_SIZE',2)
    retention.redo_runs(stamp)
    assert one('SELECT COUNT(*) n FROM pipeline_redo_items WHERE run_id=?',(ids[0],))['n']==1
    for _ in range(5):retention.redo_runs(stamp)
    assert [r['id'] for r in rows('SELECT id FROM pipeline_redo_runs ORDER BY id')]==[ids[1],*ids[3:]]
    assert one("SELECT status FROM pipeline_commands WHERE redo_id=?",(ids[1],))['status']=='queued'


def test_cleanup_caps_and_age_leave_active_work_and_business_data(client,accounts,papers,monkeypatch):
    stamp=now();old='2020-01-01T00:00:00+00:00';uid=accounts[1]['user']['id']
    with connect() as db:
        db.executemany('INSERT INTO retired_paper_sources VALUES(?,?)',[('old',old),('recent',stamp)])
        for status in ('done','failed','interrupted','processing','queued'):
            db.execute('INSERT INTO pipeline_commands(action,name,status,created_at) VALUES(?,?,?,?)',('start','classify',status,old))
        for status in ('done','failed','queued','running'):
            db.execute('''INSERT INTO source_deletion_jobs(source_keys,source_info,actor_id,status,total,created_at,updated_at,completed_at)
                VALUES('[]','[]',?,?,0,?,?,?)''',(uid,status,old,old,old))
        db.execute("INSERT INTO audit_log(action,created_at) VALUES('old',?)",(old,))
        db.execute("INSERT INTO audit_log(action,created_at) VALUES('recent',?)",(stamp,))
        db.execute("INSERT INTO trend_reference_cache VALUES('old','[]',?,NULL)",(old,))
        for n in range(4):
            db.execute('INSERT INTO llm_cache VALUES(?,?,?)',(str(n),'answer',int(datetime.fromisoformat(stamp).timestamp())+100+n))
        db.execute('INSERT INTO llm_cache VALUES(?,?,?)',('expired','answer',1))
        db.execute("INSERT INTO watches(user_id,type,value,created_at) VALUES(?,'author','Watched',?)",(uid,old))
        db.execute("INSERT INTO user_paper_state(user_id,paper_id,saved,updated_at) VALUES(?,?,1,?)",(uid,papers[0],old))
    monkeypatch.setattr(retention,'RESPONSE_CACHE_LIMIT',2)
    for operation in retention.phases(stamp):operation()
    assert [r['source_id'] for r in rows('SELECT * FROM retired_paper_sources')]==['recent']
    assert {r['status'] for r in rows('SELECT status FROM pipeline_commands')}=={'processing','queued'}
    assert {r['status'] for r in rows('SELECT status FROM source_deletion_jobs')}=={'failed','queued','running'}
    assert [r['action'] for r in rows('SELECT * FROM audit_log')]==['recent']
    assert not one('SELECT 1 FROM trend_reference_cache')
    assert {r['key'] for r in rows('SELECT key FROM llm_cache')}=={'2','3'}
    assert one('SELECT COUNT(*) n FROM papers')['n']==3
    assert one('SELECT saved FROM user_paper_state')['saved']==1
    assert one('SELECT value FROM watches')['value']=='Watched'


def test_orphan_caches_protect_papers_watches_aliases_and_failure_limits(client,accounts,papers):
    uid=accounts[1]['user']['id'];old='2020-01-01T00:00:00+00:00'
    with connect() as db:
        for ident in ('linked','watched','named','alias','failed','orphan'):
            db.execute('INSERT INTO author_impact_cache VALUES(?,?,0,2000,2020,?,1)',(ident,ident,old))
        db.execute('INSERT INTO paper_author_links VALUES(?,?,?,1)',(papers[0],'linked','linked'))
        db.execute("INSERT INTO watches(user_id,type,value,author_id,created_at) VALUES(?,'author','name','watched',?)",(uid,old))
        db.execute("INSERT INTO watches(user_id,type,value,created_at) VALUES(?,'author','NAMED',?)",(uid,old))
        db.execute("INSERT INTO author_identity_aliases VALUES('fragment','alias','W1')")
        db.execute("INSERT INTO author_query_failures VALUES('failed',3,?,?)",(old,old))
        db.execute("UPDATE papers SET abstract='Code https://github.com/Keep/Repo' WHERE id=?",(papers[0],))
        for repo in ('keep/repo','unused/repo'):
            db.execute('INSERT INTO community_repos VALUES(?,1,NULL,?,?)',(repo,old,old))
    from app.paper_index import backfill
    backfill()
    assert retention.author_cache(now())==1
    assert not one("SELECT 1 FROM author_impact_cache WHERE author_id='orphan'")
    assert one('SELECT COUNT(*) n FROM author_impact_cache')['n']==5
    assert retention.repository_cache(now())==1
    assert rows('SELECT repo FROM community_repos')==[{'repo':'keep/repo'}]
    assert one("SELECT failure_count FROM author_query_failures WHERE author_id='failed'")['failure_count']==3


def test_upgrade_defers_old_data_cleanup_and_preserves_instructions(client,accounts):
    from app.db import init_db
    uid=accounts[1]['user']['id'];item=skills.create(skills.SkillEdit(**payload(text='Keep these exact instructions')),uid)
    for _ in range(7):skills.save(item['id'],skills.SkillEdit(**payload(text='Keep these exact instructions')),uid)
    execute('INSERT INTO llm_usage(model,input_tokens,output_tokens,created_at) VALUES(?,?,?,?)',
            ('old',10,2,(datetime.fromisoformat(now())-timedelta(days=90)).isoformat()))
    with connect() as db:
        for table in ('llm_usage_daily','agent_skill_leases','skill_bundle_cleanup','retention_cursors'):
            db.execute('DROP TABLE '+table)
    init_db(recover=False)
    assert one('SELECT COUNT(*) n FROM llm_usage')['n']==1
    assert one('SELECT COUNT(*) n FROM agent_skill_versions WHERE skill_id=?',(item['id'],))['n']==8
    assert skills.detail(item['id'],uid)['text']=='Keep these exact instructions'
    for operation in retention.phases(now()):operation()
    assert one('SELECT COUNT(*) n FROM llm_usage')['n']==0
    assert one('SELECT SUM(calls) n FROM llm_usage_daily')['n']==1
    assert one('SELECT COUNT(*) n FROM agent_skill_versions WHERE skill_id=?',(item['id'],))['n']==6
    assert skills.detail(item['id'],uid)['text']=='Keep these exact instructions'


def test_count_caps_keep_newest_ties_and_do_not_count_active_commands(client,accounts,monkeypatch):
    stamp=now();uid=accounts[0]['user']['id']
    monkeypatch.setattr(retention,'COMMAND_LIMIT',2)
    monkeypatch.setattr(retention,'DELETION_LIMIT',2)
    monkeypatch.setattr(retention,'AUDIT_LIMIT',2)
    monkeypatch.setattr(retention,'TREND_CACHE_LIMIT',2)
    with connect() as db:
        for n in range(5):
            db.execute("INSERT INTO pipeline_commands(action,name,status,created_at) VALUES('start','classify','done',?)",(stamp,))
            db.execute('''INSERT INTO source_deletion_jobs(source_keys,source_info,actor_id,status,total,created_at,updated_at,completed_at)
                VALUES('[]','[]',?,'done',0,?,?,?)''',(uid,stamp,stamp,stamp))
            db.execute('INSERT INTO audit_log(action,created_at) VALUES(?,?)',(str(n),stamp))
            db.execute('INSERT INTO trend_reference_cache VALUES(?,?,?,NULL)',(str(n),'[]',stamp))
        db.execute("INSERT INTO pipeline_commands(action,name,status,created_at) VALUES('start','classify','queued',?)",(stamp,))
    for operation in retention.phases(stamp):operation()
    assert [r['id'] for r in rows('SELECT id FROM pipeline_commands ORDER BY id')]==[4,5,6]
    assert [r['id'] for r in rows('SELECT id FROM source_deletion_jobs ORDER BY id')]==[4,5]
    assert [r['action'] for r in rows('SELECT action FROM audit_log ORDER BY id')]==['3','4']
    assert {r['scope_key'] for r in rows('SELECT scope_key FROM trend_reference_cache')}=={'3','4'}


def test_cache_sweeps_continue_past_referenced_entries(client,accounts,papers,monkeypatch):
    old='2020-01-01T00:00:00+00:00'
    monkeypatch.setattr(retention,'BATCH_SIZE',2)
    with connect() as db:
        for ident in ('a','b','c'):
            db.execute('INSERT INTO author_impact_cache VALUES(?,?,0,2000,2020,?,1)',(ident,ident,old))
        for ident in ('a','b'):
            db.execute('INSERT INTO paper_author_links VALUES(?,?,?,1)',(papers[0],ident,ident))
        names=['repo/'+str(n).zfill(3) for n in range(51)]
        db.execute('UPDATE papers SET abstract=? WHERE id=?',(' '.join('https://github.com/'+repo for repo in names),papers[0]))
        for repo in [*names,'unused/repo']:
            db.execute('INSERT INTO community_repos VALUES(?,1,NULL,?,?)',(repo,old,old))
    from app.paper_index import backfill
    backfill()
    retention.author_cache(now());retention.author_cache(now())
    assert {r['author_id'] for r in rows('SELECT author_id FROM author_impact_cache')}=={'a','b'}
    retention.repository_cache(now());retention.repository_cache(now())
    assert one('SELECT COUNT(*) n FROM community_repos')['n']==51
    assert not one("SELECT 1 FROM community_repos WHERE repo='unused/repo'")


def test_usage_summaries_keep_one_year_of_history(client):
    stamp=now()
    with connect() as db:
        for days in (40,400):
            db.execute('INSERT INTO llm_usage(model,input_tokens,output_tokens,created_at) VALUES(?,?,?,?)',
                ('history',days,1,(datetime.fromisoformat(stamp)-timedelta(days=days)).isoformat()))
    for operation in retention.phases(stamp):operation()
    assert one('SELECT COUNT(*) n FROM llm_usage')['n']==0
    assert one('SELECT SUM(calls) n,SUM(input_tokens) tokens FROM llm_usage_daily')=={'n':1,'tokens':40}


def test_pipeline_skill_read_reloads_current_package_after_eviction_race(client,monkeypatch):
    item=skills.detail('quality',admin=True)
    original=skills.read_bundle;attempts=[]
    def race(data_root,ident,version_id):
        attempts.append(version_id)
        if len(attempts)==1:
            skills.save('quality',skills.SkillEdit(name=item['name'],title=item['title'],description=item['description'],
                text='Exact replacement instructions'),None,True)
            raise FileNotFoundError('old version was evicted')
        return original(data_root,ident,version_id)
    monkeypatch.setattr(skills,'read_bundle',race)
    result=skills.system_instruction('quality')
    current=skills.record('quality')
    assert attempts[0]!=current['version_id']
    assert result['text']=='Exact replacement instructions' and result['revision']==current['instruction_version']
    assert not one('SELECT 1 FROM agent_skill_leases')


def test_crashed_skill_read_protections_are_cleaned_in_small_batches(client,monkeypatch):
    version=skills.record('quality')['version_id']
    with connect() as db:
        db.executemany('INSERT INTO agent_skill_leases VALUES(?,?,?)',
            [(str(n),version,'2020-01-01T00:00:00+00:00') for n in range(6)])
    monkeypatch.setattr(retention,'BATCH_SIZE',2)
    retention.skill_versions(now())
    assert one('SELECT COUNT(*) n FROM agent_skill_leases')['n']==4
    retention.skill_versions(now());retention.skill_versions(now())
    assert not one('SELECT 1 FROM agent_skill_leases')
    assert skills.system_instruction('quality')['text']==skills.builtin()['text']
