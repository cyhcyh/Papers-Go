import re
import time
from datetime import datetime,timedelta,timezone
import httpx
from ..config import settings,today,now
from ..db import rows,connect,execute,dumps,one
from ..logs import event
from .quality import recompute
from .fetch import HEADERS
from ..task_settings import configuration


def update_signal(db,field,value,condition,parameters):
    assert field in ('hf_upvotes','github_stars')
    paper=db.execute('SELECT id,'+field+' FROM papers WHERE '+condition,parameters).fetchone()
    if not paper:return None
    if paper[field]!=value:
        db.execute('UPDATE papers SET '+field+'=? WHERE id=?',(value,paper['id']))
        recompute(db,paper['id'])
    return paper['id']


async def fetch_community():
    errors=[];processed=set();started=time.monotonic();requests=0;deferred=False
    config=configuration()['advanced']['fetch_community']
    token=config['github_token']
    budget=5000 if token else 60
    def apply_cached(ids,stars):
        if stars is not None:
            with connect() as db:
                for ident in ids:
                    if update_signal(db,'github_stars',stars,'id=?',(ident,)) is not None:processed.add(ident)
    groups={}
    for p in rows("SELECT id,abstract FROM papers WHERE published>=date(?,?)",(today(),'-'+str(config['lookback_days'])+' days')):
        match=re.search(r'https?://github\.com/([\w.-]+/[\w.-]+)',p['abstract'] or '')
        if match:
            repo=match[1].rstrip('.').removesuffix('.git').casefold()
            groups.setdefault(repo,[]).append(p['id'])
    async with httpx.AsyncClient(timeout=30,follow_redirects=True,headers=HEADERS) as client:
        try:
            response=await client.get('https://huggingface.co/api/daily_papers');response.raise_for_status()
            with connect() as db:
                for item in response.json():
                    p=item.get('paper',{})
                    if p.get('id'):
                        ident=update_signal(db,'hf_upvotes',max(0,int(p.get('upvotes',0))),'arxiv_id=?',(p['id'],))
                        if ident is not None:processed.add(ident)
        except (httpx.HTTPError,ValueError,TypeError) as error:
            errors.append('HF: '+type(error).__name__)
        for repo,ids in groups.items():
            cached=one('SELECT * FROM community_repos WHERE repo=?',(repo,))
            stars=cached['stars'] if cached else None
            if not cached or cached['next_check_at']<=now():
                if requests>=budget or time.monotonic()-started>=120:
                    deferred=True;apply_cached(ids,stars);continue
                headers={'Authorization':'Bearer '+token} if token else {}
                if cached and cached['etag']:headers['If-None-Match']=cached['etag']
                try:
                    requests+=1
                    response=await client.get('https://api.github.com/repos/'+repo,headers=headers)
                    remaining=response.headers.get('x-ratelimit-remaining')
                    if remaining is not None:budget=requests+max(0,int(remaining))
                    if response.status_code in (403,429) and (remaining=='0' or response.status_code==429 or response.headers.get('retry-after')):
                        deferred=True;budget=requests;apply_cached(ids,stars);continue
                    if response.status_code==404:
                        stars=None
                    elif response.status_code!=304:
                        response.raise_for_status();stars=max(0,int(response.json()['stargazers_count']))
                    checked=now();refresh=(datetime.now(timezone.utc)+timedelta(hours=config['cache_hours'])).isoformat()
                    execute('INSERT INTO community_repos VALUES(?,?,?,?,?) ON CONFLICT(repo) DO UPDATE SET stars=excluded.stars,etag=excluded.etag,checked_at=excluded.checked_at,next_check_at=excluded.next_check_at',
                            (repo,stars,response.headers.get('etag') or (cached['etag'] if cached else None),checked,refresh))
                except (httpx.HTTPError,KeyError,ValueError) as error:
                    errors.append('GitHub: '+type(error).__name__)
                    refresh=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
                    execute('INSERT INTO community_repos VALUES(?,?,?,?,?) ON CONFLICT(repo) DO UPDATE SET next_check_at=excluded.next_check_at',
                            (repo,stars,cached['etag'] if cached else None,now(),refresh))
            apply_cached(ids,stars)
        execute("UPDATE source_status SET progress=? WHERE name='fetch_community'",(dumps({'processed':len(processed)}),))
    event('source','社区信号更新完成，剩余仓库下次续查' if deferred else '社区信号更新完成',job='fetch_community',processed=len(processed),github_requests=requests,deferred=deferred)
    if errors:raise RuntimeError('; '.join(errors[:5]))
