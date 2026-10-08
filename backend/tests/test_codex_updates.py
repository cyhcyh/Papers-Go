import json
import httpx
import pytest
from app.config import now
from app.db import connect,one,execute
from app.llm import catalog,codex,codex_updates,runtime
from .conftest import headers
from .test_model_catalog_codex import authorize,persist


def connection(**kwargs):
    return {'id':'codex','name':'Codex','kind':'codex','base_url':codex.BASE_URL,**kwargs}


def entry(name,efforts=('low','high')):
    return {'slug':name,'visibility':'list','supported_reasoning_levels':[{'effort':v} for v in efforts]}


def seed(conn,version='0.158.0'):
    authorize()
    models=[catalog.metadata(entry('kept-model'),conn),catalog.metadata(entry('removed-model'),conn)]
    catalog.store(conn,models,compatibility={'active_version':version,'validated_at':now(),'validation_model':'kept-model'})
    return models


def transport(monkeypatch,handler):
    original=httpx.AsyncClient
    monkeypatch.setattr(codex_updates.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))


def reply(text='OK',completed=True):
    items=[{'type':'response.output_text.delta','delta':text}]
    if completed:items.append({'type':'response.completed','response':{'status':'completed','usage':{'input_tokens':5,'output_tokens':1}}})
    return httpx.Response(200,text=''.join('data: '+json.dumps(item)+'\n\n' for item in items))


def refresh(client,accounts,conn):
    return client.post('/api/admin/models/refresh',json=conn,headers=headers(accounts[0]))


def test_auto_update_validates_new_model_and_persists_version_atomically(client,accounts,monkeypatch):
    conn=connection();seed(conn);before=runtime.configuration()['routes'];calls=[]
    def handle(request):
        calls.append(request)
        if request.url.host=='registry.npmjs.org':
            assert 'authorization' not in request.headers
            return httpx.Response(200,json={'name':'@openai/codex','version':'0.160.0'})
        assert request.headers['authorization']=='Bearer private-access-token'
        if request.url.path.endswith('/models'):
            assert request.url.params['client_version']=='0.160.0'
            return httpx.Response(200,json={'models':[entry('kept-model'),entry('new-model',('low','max','ultra')),{'slug':'hidden-model','visibility':'hide'}]})
        assert request.url.path.endswith('/responses')
        body=json.loads(request.content)
        assert body['model']=='new-model' and body['reasoning']['effort']=='low'
        assert body['instructions'] and 'OK' in body['instructions']
        assert body['store'] is False and body['stream'] is True and 'tools' not in body
        return reply()
    transport(monkeypatch,handle)
    result=refresh(client,accounts,conn).json()
    assert result['ok'] and {m['id'] for m in result['models']}=={'kept-model','new-model'}
    assert result['compatibility']['active_version']=='0.160.0'
    assert result['compatibility']['latest_version']=='0.160.0'
    assert result['compatibility']['validation_model']=='new-model'
    assert catalog.saved(conn)['models']==result['models']
    assert codex_updates.state(conn)['active_version']=='0.160.0'
    assert runtime.configuration()['routes']==before
    assert len(calls)==3
    usage=one('SELECT * FROM llm_usage')
    assert usage['billing_source']=='subscription' and usage['feature']=='connection_test'
    assert 'private-access-token' not in json.dumps(result)


@pytest.mark.parametrize('failure',['http','truncated','reasoning_only'])
def test_failed_validation_keeps_old_catalog_and_version(client,accounts,monkeypatch,failure):
    conn=connection();old=seed(conn)
    def handle(request):
        if request.url.host=='registry.npmjs.org':return httpx.Response(200,json={'name':'@openai/codex','version':'0.160.0'})
        if request.url.path.endswith('/models'):return httpx.Response(200,json={'models':[entry('new-model')]})
        if failure=='http':return httpx.Response(400,json={'error':'private-access-token'})
        if failure=='truncated':return reply(completed=False)
        return httpx.Response(200,text='data: '+json.dumps({'type':'response.reasoning_text.delta','delta':'private thinking'})+'\n\ndata: '+json.dumps({'type':'response.completed','response':{'status':'completed'}})+'\n\n')
    transport(monkeypatch,handle)
    result=refresh(client,accounts,conn).json()
    assert not result['ok'] and result['models']==old
    assert result['compatibility']['active_version']=='0.158.0'
    assert result['compatibility']['latest_version']=='0.160.0'
    assert catalog.saved(conn)['models']==old
    assert codex_updates.state(conn)['active_version']=='0.158.0'
    assert 'private-access-token' not in json.dumps(result) and 'private thinking' not in json.dumps(result)


def test_manual_pin_skips_release_lookup_and_persists_setting(client,accounts,monkeypatch):
    conn=connection(codex_auto_update=False,codex_client_version='0.159.1');seed(conn);calls=[]
    def handle(request):
        calls.append(request)
        assert request.url.host!='registry.npmjs.org'
        if request.url.path.endswith('/models'):
            assert request.url.params['client_version']=='0.159.1'
            return httpx.Response(200,json={'models':[entry('kept-model')]})
        return reply()
    transport(monkeypatch,handle)
    result=refresh(client,accounts,conn).json()
    assert result['ok'] and result['compatibility']['active_version']=='0.159.1' and len(calls)==2
    config=runtime.public_configuration();config['connections'].append(conn)
    saved=client.put('/api/admin/models',json=config,headers=headers(accounts[0]))
    assert saved.status_code==200,saved.text
    settings=saved.json()['connections'][-1]
    assert settings['codex_auto_update'] is False and settings['codex_client_version']=='0.159.1'
    assert settings['catalog']['compatibility']['active_version']=='0.159.1'
    assert runtime.configuration()['connections'][-1]['codex_client_version']=='0.159.1'


def test_unchanged_version_refreshes_models_without_another_inference(client,accounts,monkeypatch):
    conn=connection();seed(conn,'0.160.0');calls=[]
    def handle(request):
        calls.append(request)
        if request.url.host=='registry.npmjs.org':return httpx.Response(200,json={'name':'@openai/codex','version':'0.160.0'})
        assert request.url.path.endswith('/models')
        return httpx.Response(200,json={'models':[entry('kept-model',('low','max'))]})
    transport(monkeypatch,handle)
    result=refresh(client,accounts,conn).json()
    assert result['ok'] and [m['id'] for m in result['models']]==['kept-model']
    assert result['models'][0]['reasoning_efforts']==['low','max'] and len(calls)==2
    assert one('SELECT COUNT(*) n FROM llm_usage')['n']==0


@pytest.mark.parametrize('bad_release',[None,{'name':'unexpected-package','version':'0.160.0'},{'name':'@openai/codex','version':'0.161.0-alpha.1'}])
def test_release_lookup_failure_uses_verified_version(client,accounts,monkeypatch,bad_release):
    conn=connection();seed(conn)
    def handle(request):
        if request.url.host=='registry.npmjs.org':
            return httpx.Response(503) if bad_release is None else httpx.Response(200,json=bad_release)
        assert request.url.path.endswith('/models') and request.url.params['client_version']=='0.158.0'
        return httpx.Response(200,json={'models':[entry('kept-model')]})
    transport(monkeypatch,handle)
    result=refresh(client,accounts,conn).json()
    assert result['ok'] and result['compatibility']['active_version']=='0.158.0'
    assert result['compatibility']['warning'] and '0.158.0' in result['message']


def test_auto_update_does_not_downgrade_and_failed_catalog_retains_results(client,accounts,monkeypatch):
    conn=connection();old=seed(conn,'0.160.0')
    def handle(request):
        if request.url.host=='registry.npmjs.org':return httpx.Response(200,json={'name':'@openai/codex','version':'0.159.1'})
        assert request.url.params['client_version']=='0.160.0'
        return httpx.Response(502)
    transport(monkeypatch,handle)
    result=refresh(client,accounts,conn).json()
    assert not result['ok'] and result['models']==old
    assert codex_updates.state(conn)['active_version']=='0.160.0' and 'HTTP 502' in result['message']


def test_login_change_during_probe_cannot_publish_catalog(client,accounts,monkeypatch):
    conn=connection();seed(conn)
    def handle(request):
        if request.url.host=='registry.npmjs.org':return httpx.Response(200,json={'name':'@openai/codex','version':'0.160.0'})
        if request.url.path.endswith('/models'):return httpx.Response(200,json={'models':[entry('new-model')]})
        execute("UPDATE codex_auth SET generation='new-login' WHERE connection_id='codex'")
        return reply()
    transport(monkeypatch,handle)
    result=refresh(client,accounts,conn).json()
    assert not result['ok'] and 'new-model' not in one('SELECT models FROM model_catalogs')['models']
    assert codex_updates.state(conn)=={}


def test_invalid_version_and_non_admin_cannot_refresh(client,accounts):
    invalid=refresh(client,accounts,connection(codex_client_version='latest;unsafe'))
    assert invalid.status_code==422
    denied=client.post('/api/admin/models/refresh',json=connection(),headers=headers(accounts[1]))
    assert denied.status_code==403


def test_save_failure_after_successful_validation_keeps_old_active_version(client,accounts,monkeypatch):
    conn=connection();old=seed(conn);previous=codex_updates.state(conn)
    def handle(request):
        if request.url.host=='registry.npmjs.org':return httpx.Response(200,json={'name':'@openai/codex','version':'0.160.0'})
        if request.url.path.endswith('/models'):return httpx.Response(200,json={'models':[entry('new-model')]})
        return reply()
    def fail_store(*args,**kwargs):raise RuntimeError('save failed')
    transport(monkeypatch,handle)
    monkeypatch.setattr(catalog,'store',fail_store)
    result=refresh(client,accounts,conn).json()
    assert not result['ok'] and result['models']==old and '目录保存失败' in result['message']
    saved=codex_updates.state(conn)
    assert saved['active_version']=='0.158.0' and saved['validation_model']=='kept-model'
    assert saved['validated_at']==previous['validated_at']
    assert catalog.saved(conn)['models']==old
