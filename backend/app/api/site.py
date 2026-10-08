from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from ..auth import admin_user
from ..config import settings, now, today
from ..db import connect
from .. import site_settings as site
from .. import recommendation_settings as recommendation
from ..logs import event
from ..llm.runtime import FEATURES
import time
import threading
from collections import OrderedDict

_overview_cache=OrderedDict()
_overview_lock=threading.Lock()

router=APIRouter()


@router.get('/api/site')
def public_site():return site.public_configuration()


@router.get('/api/site/assets/{filename}')
def site_asset(filename:str):
    if not site.ASSET_NAME.fullmatch(filename):raise HTTPException(404,'图标不存在')
    path=settings().data_dir/'site-assets'/filename
    if not path.is_file():raise HTTPException(404,'图标不存在')
    media={'png':'image/png','jpg':'image/jpeg','webp':'image/webp','ico':'image/x-icon'}[path.suffix[1:]]
    return FileResponse(path,media_type=media,headers={'X-Content-Type-Options':'nosniff','Cache-Control':'public, max-age=86400'})


@router.get('/api/admin/site',dependencies=[Depends(admin_user)])
def admin_site():return site.configuration()


@router.get('/api/admin/recommendation',dependencies=[Depends(admin_user)])
def recommendation_configuration():
    return {'value':recommendation.configuration(),'defaults':recommendation.defaults()}


@router.put('/api/admin/recommendation')
def save_recommendation(body:recommendation.RecommendationInput,user=Depends(admin_user)):
    value=recommendation.update(body)
    event('admin','推荐分权重已更新',user_id=user['id'])
    return {'value':value,'defaults':recommendation.defaults()}


@router.put('/api/admin/site')
def save_site(body:site.SiteInput,background:BackgroundTasks,user=Depends(admin_user)):
    value=site.update(body)
    # The entry is not copied to logs or public configuration.
    event('admin','网站基本设置已更新',user_id=user['id'])
    from ..pipeline.fulltext_cache import safe_cleanup
    background.add_task(safe_cleanup)
    from ..logs import safe_cleanup as clean_logs
    background.add_task(clean_logs)
    return value


@router.get('/api/admin/overview',dependencies=[Depends(admin_user)])
def overview(days:int=Query(7,ge=7,le=30)):
    if days not in (7,30):raise HTTPException(400,'请选择近 7 天或近 30 天')
    key=(str(settings().data_dir.absolute()),today(),days)
    with _overview_lock:
        saved=_overview_cache.get(key)
        if not saved or time.monotonic()-saved[0]>=10:
            saved=(time.monotonic(),_compute_overview(days));_overview_cache[key]=saved
            while len(_overview_cache)>8:_overview_cache.popitem(last=False)
        result=dict(saved[1])
    # Task state is always live, even while the aggregate statistics are cached.
    with connect() as db:
        import json
        result['jobs']=[dict(r) for r in db.execute('SELECT name,running,error,last_success FROM source_status ORDER BY name')]
        switches=db.execute("SELECT value FROM app_settings WHERE name='pipeline_enabled'").fetchone()
        enabled=json.loads(switches['value']) if switches else {}
        for job in result['jobs']:
            job['status']='running' if job['running'] else 'disabled' if not enabled.get(job['name'],True) else 'stopped' if job['error']=='已手动停止' else 'error' if job['error'] else 'ready' if job['last_success'] else 'pending'
    return result


def _compute_overview(days):
    zone=ZoneInfo(settings().tz);end=date.fromisoformat(today());start=end-timedelta(days=days-1)
    def utc(day):return datetime.combine(day,datetime.min.time(),zone).astimezone(timezone.utc).isoformat()
    def local_day(value):
        parsed=datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc).astimezone(zone).date().isoformat() if not parsed.tzinfo else parsed.astimezone(zone).date().isoformat()
    with connect() as db:
        db.create_function('local_day',1,local_day)
        total=db.execute('''SELECT
            (SELECT COUNT(*) FROM papers INDEXED BY idx_papers_display_date) n,
            (SELECT COUNT(*) FROM papers INDEXED BY idx_papers_missing_classification WHERE classified=0) unclassified,
            (SELECT COUNT(*) FROM papers INDEXED BY idx_papers_missing_vec WHERE embedding IS NULL) unembedded,
            (SELECT COUNT(*) FROM papers INDEXED BY idx_papers_missing_brief WHERE brief_json IS NULL) unbriefed''').fetchone()
        paper_days={r['day']:r['n'] for r in db.execute('SELECT substr(ingested_date,1,10) day,COUNT(*) n FROM papers WHERE ingested_date>=? GROUP BY day',(start.isoformat(),))}
        user_total=db.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        active=db.execute("SELECT COUNT(DISTINCT i.user_id) FROM interactions i JOIN users u ON u.id=i.user_id WHERE i.created_at>=? AND u.disabled=0",(utc(end-timedelta(days=6)),)).fetchone()[0]
        interactions={r['day']:dict(r) for r in db.execute("""SELECT local_day(i.created_at) day,
            COUNT(DISTINCT CASE WHEN i.action='view' OR i.view_rule=0 AND i.action IN ('like','save','skip') THEN CAST(i.user_id AS TEXT)||':'||i.paper_id END) shown,
            COUNT(DISTINCT i.user_id) active_users,SUM(i.action='like') liked,SUM(i.action='save') saved,
            SUM(i.action IN ('like','save','skip','expand')) actions
            FROM interactions i WHERE i.created_at>=? AND i.action IN ('view','like','save','skip','expand')
            AND NOT EXISTS(SELECT 1 FROM interactions u WHERE u.target_id=i.id AND u.action='undo') GROUP BY day""",(utc(start),))}
        usage={r['day']:dict(r) for r in db.execute('SELECT local_day(created_at) day,SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens,SUM(usage_known=0) unknown_usage,COUNT(*) calls FROM llm_usage WHERE billing_source="api" AND created_at>=? GROUP BY day',(utc(start),))}
        by_model=[dict(r) for r in db.execute('SELECT model,SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens,SUM(usage_known=0) unknown_usage,COUNT(*) calls FROM llm_usage WHERE billing_source="api" AND created_at>=? GROUP BY model ORDER BY SUM(input_tokens+output_tokens) DESC',(utc(start),))]
        by_feature=[{**dict(r),'label':FEATURES.get(r['feature'],('历史 / 未标注功能',))[0]} for r in db.execute('SELECT feature,SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens,SUM(usage_known=0) unknown_usage,COUNT(*) calls FROM llm_usage WHERE billing_source="api" AND created_at>=? GROUP BY feature ORDER BY SUM(input_tokens+output_tokens) DESC',(utc(start),))]
        by_connection=[dict(r) for r in db.execute('SELECT connection_id,SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens,COUNT(*) calls FROM llm_usage WHERE billing_source="api" AND created_at>=? GROUP BY connection_id ORDER BY SUM(input_tokens+output_tokens) DESC',(utc(start),))]
        subscription=[dict(r) for r in db.execute("SELECT model,SUM(input_tokens) input_tokens,SUM(output_tokens) output_tokens,SUM(usage_known=0) unknown_usage,COUNT(*) calls FROM llm_usage WHERE billing_source='subscription' AND created_at>=? GROUP BY model",(utc(start),))]
        import json
        saved=db.execute("SELECT value FROM app_settings WHERE name='models'").fetchone()
        connection_names={c['id']:c['name'] for c in json.loads(saved['value'])['connections']} if saved else {'local':'本地 Ollama','cloud':'默认云端'}
        for row in by_connection:row['label']=connection_names.get(row['connection_id'],row['connection_id'] or '历史 / 未标注连接')
        categories=[dict(r) for r in db.execute('SELECT s.code,s.kind,COUNT(p.paper_id) count FROM source_categories s LEFT JOIN paper_categories p ON p.category_key=LOWER(s.key) GROUP BY s.key ORDER BY count DESC,s.sort_order')]
        jobs=[dict(r) for r in db.execute('SELECT name,running,error,last_success FROM source_status ORDER BY name')]
        switches=db.execute("SELECT value FROM app_settings WHERE name='pipeline_enabled'").fetchone()
        enabled=json.loads(switches['value']) if switches else {}
        for job in jobs:
            job['status']='running' if job['running'] else 'disabled' if not enabled.get(job['name'],True) else 'stopped' if job['error']=='已手动停止' else 'error' if job['error'] else 'ready' if job['last_success'] else 'pending'
        proposals=db.execute("SELECT COUNT(*) FROM topics WHERE status='proposed'").fetchone()[0]
        registrations={r['day']:r['n'] for r in db.execute('SELECT local_day(created_at) day,COUNT(*) n FROM users WHERE created_at>=? GROUP BY day',(utc(start),))}
    timeline=[]
    for n in range(days):
        day=(start+timedelta(days=n)).isoformat();i=interactions.get(day,{});u=usage.get(day,{})
        timeline.append({'date':day,'papers':paper_days.get(day,0),'registrations':registrations.get(day,0),**{k:i.get(k,0) for k in ('shown','active_users','liked','saved','actions')},**{k:u.get(k,0) or 0 for k in ('input_tokens','output_tokens','calls','unknown_usage')}})
    current=timeline[-1]
    return {'days':days,'updated_at':now(),'totals':{'papers':total['n'],'users':user_total,'active_users_7d':active,
        'today_papers':current['papers'],'today_shown':current['shown'],'today_actions':current['actions'],
        'today_input_tokens':current['input_tokens'],'today_output_tokens':current['output_tokens'],'today_unknown_usage':current['unknown_usage']},
        'pending':{'classification':total['unclassified'] or 0,'embedding':total['unembedded'] or 0,'brief':total['unbriefed'] or 0,'topics':proposals},
        'subscription_usage':subscription,'timeline':timeline,'categories':categories,'by_model':by_model,'by_feature':by_feature,'by_connection':by_connection,'jobs':jobs}
