import asyncio
import pytest
from app.config import now,today
from app.db import execute, one, init_db, dumps
from app.llm import runtime
from app.llm.secrets import encrypt_configuration
from app import scheduler
from app.task_settings import defaults as task_defaults
from app.pipeline_control import set_job_enabled
from .conftest import headers


def unconfigure():
    execute("DELETE FROM app_settings WHERE name='models'")


def store(config):
    execute("INSERT OR REPLACE INTO app_settings VALUES('models',?,?)",(dumps(encrypt_configuration(config)),now()))


def test_fresh_models_and_registration_do_not_select_or_run(client,monkeypatch):
    unconfigure()
    calls=[]
    monkeypatch.setattr('app.api.admin.start_manual',calls.append)
    admin=client.post('/api/auth/register',json={'username':'freshadmin','password':'secret123'}).json()
    config=client.get('/api/admin/models',headers=headers(admin)).json()
    assert admin['admin_entry'] and admin['user']['is_admin']
    assert config['connections']==[]
    assert all(not r['primary']['model'] and not r['primary']['connection_id'] and r['fallback'] is None for r in config['routes'].values())
    assert not calls and not runtime.configured('embedding')
    assert all(s['enabled'] for s in task_defaults()['schedules'].values())
    assert client.get('/api/admin/llm-status',headers=headers(admin)).status_code==200
    # Adding papers before selecting a model must not resurrect legacy defaults.
    execute('INSERT INTO papers(title,authors,created_at,ingested_date) VALUES(?,?,?,?)',('manually fetched','[]',now(),today()))
    init_db()
    assert runtime.configuration()['connections']==[]


@pytest.mark.asyncio
async def test_manual_scheduled_and_bootstrap_share_preflight(client,accounts,monkeypatch):
    unconfigure()
    admin,_=accounts
    response=client.post('/api/admin/jobs/pipeline',headers=headers(admin))
    assert response.status_code==409
    assert response.json()['detail']['code']=='model_configuration_missing'
    missing={item['feature'] for item in response.json()['detail']['missing']}
    assert {'classify','embedding','quality','brief','reading_l2'}<=missing
    assert 'chat' not in missing and 'audit' not in missing
    async def forbidden(*args,**kwargs):raise AssertionError('no fetching or model probes before setup')
    monkeypatch.setattr(scheduler,'_stages',forbidden)
    monkeypatch.setattr(scheduler.ollama,'status',forbidden)
    await scheduler.run_scheduled('pipeline')
    await scheduler.bootstrap()
    assert one("SELECT error FROM source_status WHERE name='pipeline'")['error'].startswith('待配置：')
    snapshots=client.get('/api/admin/sources',headers=headers(admin)).json()
    assert next(s for s in snapshots if s['name']=='classify')['missing_configuration']


def test_manual_fetch_and_disabled_tasks_do_not_require_unrelated_models(client,accounts,monkeypatch):
    unconfigure()
    admin,_=accounts
    calls=[]
    monkeypatch.setattr('app.api.admin.start_manual',calls.append)
    assert client.post('/api/admin/jobs/fetch_arxiv',headers=headers(admin)).status_code==200
    assert calls==['fetch_arxiv']
    for name in ('classify','build_vectors','assess_quality','tldr_gen','preread'):set_job_enabled(name,False)
    assert client.post('/api/admin/jobs/pipeline',headers=headers(admin)).status_code==200
    assert calls==['fetch_arxiv','pipeline']


@pytest.mark.asyncio
async def test_schedule_runs_configured_site_without_a_first_manual_run(client,monkeypatch):
    config=runtime.defaults()
    config['connections']=[{'id':'actual','kind':'ollama','name':'Actual','base_url':'http://models.test','api_key':''}]
    for feature in ('classify','embedding'):
        config['routes'][feature]['primary'].update(connection_id='actual',model='actual-'+feature)
    store(config)
    calls=[]
    async def fetch():calls.append('fetch')
    async def classify():calls.append('classify')
    monkeypatch.setattr(scheduler,'jobs',{'fetch_arxiv':fetch,'classify':classify})
    monkeypatch.setattr(scheduler,'_pipeline_lock',asyncio.Lock())
    await scheduler.run_scheduled('pipeline')
    assert calls==['fetch','classify']


def test_connections_and_features_can_be_saved_incrementally(client,accounts,monkeypatch):
    unconfigure()
    admin,_=accounts
    config=client.get('/api/admin/models',headers=headers(admin)).json()
    config['connections']=[{'id':'actual','kind':'cloud','name':'API','base_url':'https://models.test/v1','api_key':'private-test-key'}]
    calls=[]
    async def discover(binding):
        calls.append(binding['model'])
        return {'thinking_modes':['auto'],'reasoning_efforts':[],'type':'chat','note':''}
    monkeypatch.setattr('app.api.model_config.discover',discover)
    response=client.put('/api/admin/models',headers=headers(admin),json=config)
    assert response.status_code==200 and not response.json()['rebuild_queued']
    assert not calls and 'private-test-key' not in response.text
    config=response.json()
    config['routes']['brief']['primary'].update(connection_id='actual',model='my-text-model')
    response=client.put('/api/admin/models',headers=headers(admin),json=config)
    assert response.status_code==200 and calls==['my-text-model']
    assert not response.json()['routes']['embedding']['primary']['model']


def test_first_embedding_does_not_queue_a_rebuild(client,accounts,monkeypatch):
    unconfigure()
    admin,_=accounts
    config=client.get('/api/admin/models',headers=headers(admin)).json()
    config['connections']=[{'id':'actual','kind':'ollama','name':'Actual','base_url':'http://models.test','api_key':''}]
    config['routes']['embedding']['primary'].update(connection_id='actual',model='my-embedding')
    calls=[]
    monkeypatch.setattr('app.api.admin.start_manual',calls.append)
    response=client.put('/api/admin/models',headers=headers(admin),json=config)
    assert response.status_code==200 and not response.json()['rebuild_queued'] and not calls
    assert runtime.embedding_identity(runtime.configuration()) is not None


def test_existing_implicit_configuration_is_preserved_once(client,accounts):
    unconfigure()
    execute("DELETE FROM app_migrations WHERE name='model_explicit_selection_v1'")
    init_db()
    assert runtime.configuration()['routes']['embedding']['primary']['model']==runtime.legacy_defaults()['routes']['embedding']['primary']['model']
    config=runtime.configuration()
    config['routes']['brief']['primary']['model']='administrator-choice'
    store(config)
    init_db()
    assert runtime.configuration()['routes']['brief']['primary']['model']=='administrator-choice'


@pytest.mark.asyncio
@pytest.mark.parametrize('only_preread',[False,True])
async def test_bootstrap_cloud_models_and_unused_empty_routes(client,papers,monkeypatch,only_preread):
    config=runtime.defaults()
    config['connections']=[{'id':'api','kind':'cloud','name':'API','base_url':'https://models.test/v1','api_key':'offline-test-key'}]
    features=('reading_l2',) if only_preread else ('classify','embedding','quality','brief','reading_l2')
    for feature in features:config['routes'][feature]['primary'].update(connection_id='api',model='configured-'+feature)
    store(config)
    if only_preread:
        for name in ('classify','build_vectors','assess_quality','tldr_gen'):set_job_enabled(name,False)
    calls=[]
    async def run_job(name):calls.append(name)
    async def forbidden():raise AssertionError('cloud-only startup must not probe Ollama')
    monkeypatch.setattr(scheduler,'run_job',run_job)
    monkeypatch.setattr(scheduler.ollama,'status',forbidden)
    await scheduler.bootstrap()
    assert 'preread' in calls
    assert ('classify' in calls) is (not only_preread)


def test_empty_fallback_is_not_saved_as_an_executable_model(client,accounts):
    unconfigure()
    admin,_=accounts
    config=client.get('/api/admin/models',headers=headers(admin)).json()
    config['routes']['brief']['fallback']=dict(config['routes']['brief']['primary'])
    response=client.put('/api/admin/models',headers=headers(admin),json=config)
    assert response.status_code==200
    assert response.json()['routes']['brief']['fallback'] is None
    config['routes']['brief']['primary']['connection_id']='half-selected'
    assert client.put('/api/admin/models',headers=headers(admin),json=config).status_code==422
