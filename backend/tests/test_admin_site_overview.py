import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from app.config import settings,today,now
from app.db import execute,one
from app.site_settings import configuration
from app.llm.provider import cloud
from app.llm.runtime import bind
from .conftest import headers


def test_entry_is_server_owned_and_old_entry_is_closed(client,accounts):
    admin,regular=accounts
    entry=configuration()['admin_path']
    public=client.get('/api/site')
    assert public.status_code==200 and 'admin_path' not in public.json()
    assert entry not in client.get('/').text
    assert client.get('/admin').status_code==404
    assert client.get('/admin/models').status_code==404
    page=client.get(entry+'/overview')
    assert page.status_code==200 and f'content="{entry}"' in page.text
    assert 'no-store' in page.headers['cache-control']
    assert client.get('/api/admin/site').status_code==401
    assert client.get('/api/admin/site',headers=headers(regular)).status_code==403
    body=client.get('/api/admin/site',headers=headers(admin)).json()
    body['admin_path']='/my-private-console'
    result=client.put('/api/admin/site',json=body,headers=headers(admin))
    assert result.status_code==200
    assert client.get(entry).status_code==404 and client.get(entry+'/settings').status_code==404
    assert client.get('/my-private-console/settings').status_code==200
    assert '/my-private-console' not in client.get('/api/site').text
    assert not one("SELECT 1 FROM app_logs WHERE message LIKE '%my-private-console%' OR detail LIKE '%my-private-console%'")


def test_branding_escapes_html_and_upload_is_persistent(client,accounts):
    admin,_=accounts
    body=configuration();body['name']='<研究 & 发现>';body['description']='"内容" <script>alert(1)</script>'
    image='iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3xkAAAAASUVORK5CYII='
    body['logo_upload']={'data':image};body['favicon_upload']={'data':image}
    result=client.put('/api/admin/site',json=body,headers=headers(admin))
    assert result.status_code==200
    value=result.json();logo=value['logo_url'];favicon=value['favicon_url']
    assert logo!=favicon and client.get(logo).content==base64.b64decode(image)
    assert client.get(logo).headers['content-type']=='image/png'
    html=client.get('/').text
    assert '<title>&lt;研究 &amp; 发现&gt;</title>' in html and 'href="'+favicon+'"' in html
    assert '<script>alert(1)</script>' not in html
    body=value;body['logo_url']='';body['favicon_url']=''
    assert client.put('/api/admin/site',json=body,headers=headers(admin)).status_code==200
    assert client.get(logo).status_code==404 and client.get(favicon).status_code==404


def test_invalid_paths_and_external_assets_do_not_change_configuration(client,accounts):
    admin,_=accounts;initial=configuration()
    for path in ['/admin','/settings','/api','/assets','/ab','//console-name','/console/name','/console?key=1','https://site.example/admin']:
        body={**initial,'admin_path':path}
        assert client.put('/api/admin/site',json=body,headers=headers(admin)).status_code==422
    for url in ['https://example.com/image.png','/api/site/assets/../../secret','/icon.svg']:
        assert client.put('/api/admin/site',json={**initial,'logo_url':url},headers=headers(admin)).status_code==400
    assert client.put('/api/admin/site',json={**initial,'logo_upload':{'data':base64.b64encode(b'<svg><script/></svg>').decode()}},headers=headers(admin)).status_code==400
    assert configuration()==initial


def test_admin_login_accepts_only_admin_accounts(client,accounts):
    admin,regular=accounts
    assert client.post('/api/auth/admin-login',json={'username':'bob','password':'secret123'}).status_code==401
    result=client.post('/api/auth/admin-login',json={'username':'alice','password':'secret123'})
    assert result.status_code==200 and result.json()['user']['is_admin']
    assert client.get('/api/admin/overview',headers=headers(regular)).status_code==403


def test_entry_is_created_during_first_registration_and_returned_once(client):
    assert configuration()['admin_path']==''
    first=client.post('/api/auth/register',json={'username':'administrator','password':'secret123'}).json()
    assert first['user']['is_admin'] and first['admin_entry'].startswith('/console-')
    assert len(first['admin_entry'])>25 and configuration()['admin_path']==first['admin_entry']
    second=client.post('/api/auth/register',json={'username':'researcher','password':'secret123'}).json()
    assert 'admin_entry' not in second and not second['user']['is_admin']
    assert 'admin_entry' not in client.post('/api/auth/login',json={'username':'administrator','password':'secret123'}).json()
    assert 'admin_entry' not in client.post('/api/auth/admin-login',json={'username':'administrator','password':'secret123'}).json()
    assert 'admin_entry' not in client.post('/api/auth/refresh',json={'refresh_token':first['refresh_token']}).json()
    assert first['admin_entry'] not in client.get('/api/auth/config').text
    assert client.get('/api/admin/site',headers=headers(first)).json()['admin_path']==first['admin_entry']


def test_overview_uses_local_days_and_keeps_unknown_and_legacy_usage(client,accounts,papers):
    admin,_=accounts
    local=datetime.fromisoformat(today()).replace(tzinfo=ZoneInfo(settings().tz))+timedelta(minutes=5)
    timestamp=local.astimezone(timezone.utc).isoformat()
    execute('INSERT INTO llm_usage(model,input_tokens,output_tokens,created_at) VALUES(?,?,?,?)',('old-model',100,30,timestamp))
    with bind({'kind':'cloud','id':'cloud','feature':'classify'}):
        cloud.record_usage('new-model',SimpleNamespace(prompt_tokens=20,completion_tokens=5))
        cloud.record_usage('new-model',None)
    with bind({'kind':'ollama','id':'local','feature':'chat'}):cloud.record_usage('local-model',SimpleNamespace(prompt_tokens=999,completion_tokens=999))
    execute("INSERT INTO interactions(user_id,paper_id,action,dwell_ms,created_at,view_rule) VALUES(?,?, 'view',5000,?,1)",(admin['user']['id'],papers[0],timestamp))
    execute("INSERT INTO source_status(name,error) VALUES('classify','已手动停止')")
    execute("INSERT INTO interactions(user_id,paper_id,action,dwell_ms,created_at,view_rule) VALUES(?,?, 'view',5000,?,1)",(admin['user']['id'],papers[0],timestamp))
    report=client.get('/api/admin/overview?days=7',headers=headers(admin)).json()
    assert len(report['timeline'])==7 and report['timeline'][-1]['date']==today()
    assert report['totals']['today_input_tokens']==120 and report['totals']['today_output_tokens']==35
    assert report['totals']['today_unknown_usage']==1 and report['totals']['today_shown']==1
    assert report['totals']['papers']==3 and report['totals']['users']==2
    assert next(j for j in report['jobs'] if j['name']=='classify')['status']=='stopped'
    assert next(c for c in report['categories'] if c['code']=='cs.AI')['count']==3
    assert {item['model'] for item in report['by_model']}=={'old-model','new-model'}
    assert {item['label'] for item in report['by_feature']}=={'历史 / 未标注功能','主题分类'}
    assert one("SELECT feature,connection_id,usage_known FROM llm_usage WHERE model='new-model' AND usage_known=1")=={'feature':'classify','connection_id':'cloud','usage_known':1}
    assert len(client.get('/api/admin/overview?days=30',headers=headers(admin)).json()['timeline'])==30
    assert client.get('/api/admin/overview?days=8',headers=headers(admin)).status_code==400
