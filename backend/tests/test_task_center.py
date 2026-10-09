import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from app import scheduler, task_settings
from app.config import now, settings, today
from app.db import execute, one, rows
from app.paper_lifecycle import expiry_after
from app.pipeline_control import set_job_enabled
from .conftest import headers

BASE = '/api/admin/task-center'


def test_settings_are_admin_only_and_defaults_match_schedules(client, accounts):
    admin, user = accounts
    assert client.get(BASE).status_code == 401
    assert client.get(BASE, headers=headers(user)).status_code == 403
    value = client.get(BASE, headers=headers(admin)).json()
    assert value['timezone'] == 'Asia/Shanghai'
    assert {name:plan['time'] for name,plan in value['schedules'].items()} == {
        'pipeline':'10:00','author_impact':'06:20','paper_expiry':'11:00',
        'trend_report':'10:50','reflect':'04:00','audit':'06:00'}
    assert all(plan['enabled'] for plan in value['schedules'].values())
    assert value['schedules']['reflect']['frequency'] == 'weekly'
    assert value['schedules']['reflect']['weekdays'] == [6]
    assert value['schedules']['audit']['frequency'] == 'weekly'
    assert 'github_token' not in value['advanced']['fetch_community']
    assert 'github_token' not in value['defaults']['advanced']['fetch_community']
    assert client.patch(BASE+'/schedules', json={}, headers=headers(user)).status_code == 403


def test_credentials_encrypt_keep_replace_clear_and_never_echo(client, accounts, monkeypatch):
    admin = headers(accounts[0])
    monkeypatch.setattr(settings(), 'github_token', 'environment-token')
    path = BASE+'/advanced/fetch_community'
    result = client.patch(path, json={'github_token':'private-test-token','lookback_days':7}, headers=admin)
    assert result.status_code == 200
    assert 'private-test-token' not in result.text
    stored = one("SELECT value FROM app_settings WHERE name='task_center'")['value']
    assert 'private-test-token' not in stored and 'environment-token' not in stored
    assert task_settings.configuration()['advanced']['fetch_community']['github_token'] == 'private-test-token'
    assert client.patch(path, json={'github_token':'','cache_hours':48}, headers=admin).status_code == 200
    assert task_settings.configuration()['advanced']['fetch_community']['github_token'] == 'private-test-token'
    assert client.patch(path, json={'github_token':'replacement-token'}, headers=admin).status_code == 200
    assert task_settings.configuration()['advanced']['fetch_community']['github_token'] == 'replacement-token'
    value = client.patch(path, json={'clear_secret':True}, headers=admin).json()
    assert not value['advanced']['fetch_community']['github_token_configured']
    assert task_settings.configuration()['advanced']['fetch_community']['github_token'] == ''
    assert not any('private-test-token' in row['detail'] for row in rows('SELECT detail FROM app_logs'))


def test_invalid_parameters_are_atomic_and_secret_validation_is_redacted(client, accounts):
    admin = headers(accounts[0])
    before = client.get(BASE, headers=admin).json()['advanced']
    assert client.patch(BASE+'/advanced/paper_expiry', json={'batch_size':999999}, headers=admin).status_code == 422
    response = client.patch(BASE+'/advanced/fetch_community', json={'github_token':'do-not-echo','cache_hours':0}, headers=admin)
    assert response.status_code == 422 and 'do-not-echo' not in response.text
    assert client.patch(BASE+'/advanced/audit', json={'run_seconds':100}, headers=admin).status_code == 422
    assert client.get(BASE, headers=admin).json()['advanced'] == before


@pytest.mark.asyncio
async def test_plan_refresh_replaces_only_changed_jobs_without_running_tasks(client, accounts):
    timer = scheduler.make_scheduler()
    original = timer.get_job('paper_expiry')
    assert timer.get_job('fetch_arxiv') is None  # Main stages no longer have duplicate cron entries.
    assert client.patch(BASE+'/schedules', json={'pipeline':{'enabled':False,'time':'13:30'}, 'audit':{'frequency':'weekly','weekdays':[0,4],'time':'08:40'}}, headers=headers(accounts[0])).status_code == 200
    await scheduler.refresh_schedules(timer)
    assert timer.get_job('pipeline') is None
    assert timer.get_job('paper_expiry') is original
    assert str(timer.get_job('audit').trigger.fields[4]) == '0,4'
    assert str(timer.get_job('audit').trigger.fields[-3]) == '8'
    assert not scheduler.job_state()['busy']
    assert client.patch(BASE+'/schedules', json={'audit':{'frequency':'weekly','weekdays':[]}}, headers=headers(accounts[0])).status_code == 422
    # Changes to another group preserve previously saved schedules.
    client.patch(BASE+'/advanced/audit', json={'sample_size':4}, headers=headers(accounts[0]))
    await scheduler.refresh_schedules(timer)
    assert timer.get_job('pipeline') is None


@pytest.mark.asyncio
async def test_full_pipeline_excludes_independent_jobs_and_skips_disabled_stages(client, monkeypatch):
    monkeypatch.setattr(scheduler, '_pipeline_lock', asyncio.Lock())
    calls = []
    def stage(name):
        async def run():
            calls.append(name)
        return run
    monkeypatch.setattr(scheduler, 'jobs', {name:stage(name) for name in (*task_settings.PIPELINE_JOBS,*task_settings.INDEPENDENT_JOBS)})
    set_job_enabled('classify', False)
    await scheduler.pipeline()
    assert calls == [name for name in task_settings.PIPELINE_JOBS if name != 'classify']


@pytest.mark.asyncio
async def test_scheduled_trigger_does_not_duplicate_manual_work(client, monkeypatch):
    pending = asyncio.get_running_loop().create_future()
    monkeypatch.setattr(scheduler, '_job_tasks', {'audit': {pending}})
    calls = []
    async def run(name):
        calls.append(name)
    monkeypatch.setattr(scheduler, 'run_job', run)
    try:
        await scheduler.run_scheduled('audit')
        assert not calls
    finally:
        pending.cancel()


def test_lifetime_changes_apply_to_new_papers_and_feedback_only(client, accounts, papers):
    previous = one('SELECT expires_at FROM papers WHERE id=?', (papers[0],))['expires_at']
    response = client.patch(BASE+'/advanced/paper_expiry', json={'initial_days':30,'social_days':60,'repeat_limit':1}, headers=headers(accounts[0]))
    assert response.status_code == 200
    assert one('SELECT expires_at FROM papers WHERE id=?',(papers[0],))['expires_at'] == previous
    stamp = now()
    ident = execute('INSERT INTO papers(title,created_at,ingested_date,published) VALUES(?,?,?,?)', ('New paper',stamp,today(),today()))
    assert one('SELECT expires_at FROM papers WHERE id=?',(ident,))['expires_at'] == expiry_after(stamp,30)
    action = lambda name: client.post('/api/interactions',json={'paper_id':ident,'action':name},headers=headers(accounts[0])).json()
    assert action('like')['life_extended']
    action('remove_like')
    assert action('like')['life_extended']
    action('remove_like')
    assert not action('like')['life_extended']
    assert action('save')['life_extended']  # First save is still free of the repeat budget.


@pytest.mark.asyncio
async def test_audit_uses_configured_sample_bound(client, papers, accounts, monkeypatch):
    from app.pipeline import metrics
    captured = []
    async def complete(feature,messages,**kwargs):
        captured.extend(json.loads(messages[-1]['content']))
        return {'checks':[]}
    monkeypatch.setattr(metrics.models, 'complete', complete)
    client.patch(BASE+'/advanced/audit',json={'sample_size':1},headers=headers(accounts[0]))
    await metrics.audit()
    assert len(captured) == 1


def test_metric_user_choices_are_bounded_and_prefix_searchable(client, accounts):
    for index in range(40):
        execute('INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)', (f'metric{index:02}', 'unused', now()))
    value = client.get(BASE+'/metric-users',headers=headers(accounts[0])).json()
    assert len(value) == 30
    value = client.get(BASE+'/metric-users?query=metric39',headers=headers(accounts[0])).json()
    assert [user['username'] for user in value] == ['metric39']


def test_background_status_read_does_not_block_foreground_requests(client, accounts, monkeypatch):
    from app.api import admin
    started, release = threading.Event(), threading.Event()
    def snapshot():
        started.set()
        assert release.wait(3)
        return []
    monkeypatch.setattr(settings(), 'pipeline_mode', 'external')
    monkeypatch.setattr(admin, 'source_snapshot', snapshot)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.get, '/api/admin/sources', headers=headers(accounts[0]))
        assert started.wait(2)
        try:
            assert client.get('/api/health').status_code == 200
            assert not pending.done()
        finally:
            release.set()
        assert pending.result().status_code == 200
