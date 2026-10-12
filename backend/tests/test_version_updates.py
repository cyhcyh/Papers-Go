import asyncio

import httpx
import pytest

from app import versioning
from .conftest import headers


@pytest.fixture(autouse=True)
def isolated_version_cache(monkeypatch):
    monkeypatch.setattr(versioning, '_cache', versioning.OrderedDict())
    monkeypatch.setattr(versioning, '_locks', {kind: asyncio.Lock() for kind in ('version', 'changelog')})


@pytest.mark.parametrize('value,expected', [('1.0.0',(1,0,0)),('v1.10.2\n',(1,10,2)),('0.0.1',(0,0,1))])
def test_version_parsing(value,expected):
    assert versioning.version_parts(value)==expected


@pytest.mark.parametrize('value', ['1.0','1.0.0-beta','1.0.0\n1.1.0','<html>1.0.0</html>','-1.0.0',None])
def test_invalid_versions_are_not_treated_as_updates(value):
    with pytest.raises(ValueError):versioning.version_parts(value)


def test_changelog_is_bounded_and_preserves_text_without_executing_html():
    content='## [1.2.0] - 2026-10-12\n### 新增\n- <script>alert(1)</script>\n### 优化\n* 更简洁的排版\n### 修复\n- 修复显示\n'
    content+='## [1.2.0] - 2026-10-12\n### 新增\n- 重复版本\n'
    content+='## [1.3.0] - 2026-99-99\n### 新增\n- 日期无效\n'
    content+=''.join(f'## [1.1.{n}] - 2026-10-11\n### Fixed\n- item {n}\n' for n in range(30))
    entries=versioning.parse_changelog(content)
    assert len(entries)==10
    assert entries[0]=={'version':'1.2.0','date':'2026-10-12','added':['<script>alert(1)</script>'],'changed':['更简洁的排版'],'fixed':['修复显示']}
    assert entries[-1]['fixed']==['item 8']


@pytest.mark.asyncio
async def test_shared_six_hour_cache_two_slots_and_manual_refresh(monkeypatch):
    calls=[]
    async def download(kind):
        calls.append(kind)
        return '999.0.0' if kind=='version' else '## [999.0.0] - 2026-10-12\n### 新增\n- 新功能'
    monkeypatch.setattr(versioning,'_download',download)
    first=await versioning.check_version()
    assert first['has_update'] and first['latest_version']=='999.0.0'
    assert (await versioning.check_version())==first
    assert (await versioning.remote_changelog())['status']=='ok'
    assert (await versioning.remote_changelog())['changelog'][0]['version']=='999.0.0'
    assert calls==['version','changelog'] and len(versioning._cache)==2
    await versioning.check_version(force=True)
    assert calls==['version','changelog','version'] and len(versioning._cache)==2
    versioning._cache['version']['cached_at']-=versioning.CACHE_SECONDS+1
    await versioning.check_version()
    assert calls==['version','changelog','version','version']


@pytest.mark.asyncio
async def test_concurrent_checks_share_one_request_including_manual_refresh(monkeypatch):
    calls=[]
    async def download(kind):
        calls.append(kind)
        await asyncio.sleep(.02)
        return '999.0.0'
    monkeypatch.setattr(versioning,'_download',download)
    results=await asyncio.gather(*(versioning.check_version(force=True) for _ in range(12)))
    assert calls==['version']
    assert all(result==results[0] for result in results)


@pytest.mark.asyncio
async def test_old_remote_version_does_not_offer_a_downgrade(monkeypatch):
    async def download(kind):return '0.0.1'
    monkeypatch.setattr(versioning,'_download',download)
    result=await versioning.check_version()
    assert result['status']=='ok' and not result['has_update']


@pytest.mark.asyncio
async def test_timeout_and_bad_files_do_not_claim_latest_or_drop_last_valid_value(monkeypatch):
    async def download(kind):return '999.0.0'
    monkeypatch.setattr(versioning,'_download',download)
    await versioning.check_version()
    async def fail(kind):raise asyncio.TimeoutError()
    monkeypatch.setattr(versioning,'_download',fail)
    result=await versioning.check_version(force=True)
    assert result['status']=='failed' and result['has_update'] and result['latest_version']=='999.0.0'
    assert (await versioning.remote_changelog())['status']=='failed'
    assert len(versioning._cache)==2
    versioning._cache.clear()
    async def malformed(kind):return '<html>Gateway error</html>'
    monkeypatch.setattr(versioning,'_download',malformed)
    result=await versioning.check_version()
    assert result['status']=='failed' and result['latest_version'] is None and not result['has_update']
    assert (await versioning.remote_changelog())['changelog']==[]


@pytest.mark.asyncio
async def test_remote_download_is_fixed_public_bounded_and_unauthenticated(monkeypatch):
    requests=[]
    def handle(request):
        requests.append(request)
        return httpx.Response(200,text='1.0.1' if len(requests)==1 else 'x'*65)
    original=httpx.AsyncClient
    monkeypatch.setattr(versioning.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    assert await versioning._download('version')=='1.0.1'
    with pytest.raises(ValueError,match='读取上限'):await versioning._download('version')
    assert all(str(request.url)=='https://raw.githubusercontent.com/cyhcyh/Papers-Go/main/VERSION.txt' for request in requests)
    assert all('authorization' not in request.headers for request in requests)


def test_public_installed_metadata_never_checks_remote_and_admin_routes_are_protected(client,accounts,monkeypatch):
    calls=[]
    async def download(kind):
        calls.append(kind)
        return '999.0.0' if kind=='version' else '## [999.0.0] - 2026-10-12\n### 新增\n- 新功能'
    monkeypatch.setattr(versioning,'_download',download)
    response=client.get('/api/version')
    assert response.status_code==200 and response.json()['current_version']==versioning.CURRENT_VERSION
    assert response.json()['changelog'] and not calls
    admin,user=accounts
    for path in ('/api/admin/version','/api/admin/version/changelog'):
        assert client.get(path).status_code==401
        assert client.get(path,headers=headers(user)).status_code==403
    assert not calls
    assert client.get('/api/admin/version',headers=headers(admin)).json()['has_update']
    assert client.get('/api/admin/version',headers=headers(admin)).status_code==200
    assert calls==['version']
    assert client.get('/api/admin/version/changelog',headers=headers(admin)).json()['changelog'][0]['version']=='999.0.0'
    assert client.get('/api/admin/version?force=true',headers=headers(admin)).status_code==200
    assert calls==['version','changelog','version'] and len(versioning._cache)==2
