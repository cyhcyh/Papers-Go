import json
from datetime import datetime, timezone, timedelta, date
from typing import Literal
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from ..auth import current_user, optional_user
from ..config import now, today
from ..db import connect, rows, one, execute, dumps
from ..interest.profile import current
from ..interest.online import update_profile, restore_part
from ..pipeline.score import feed, scored_papers, serialize_paper, balanced_papers, ranked_page, browse_page
from ..pipeline.read import request_card, card_response, card_events
from ..pipeline.trends import trend_data, trend_summary
from ..pipeline.direction_trends import request_update
from ..catalog import directory, validate_category_keys
from ..paper_lifecycle import extend_life, social_state
from .. import vector_store

router = APIRouter(prefix='/api')
VIEW_DWELL_MS = 5000


@router.get('/categories')
def categories():
    return directory()


@router.get('/recommendations')
def recommendations(user=Depends(optional_user)):
    return {'items':ranked_page(user['id'] if user else None,'recommendations',0,5)['items']}


@router.get('/topics')
def topics():
    return rows("SELECT t.*,COUNT(CASE WHEN p.ingested_date=? THEN p.id END) AS today_count,COUNT(p.id) AS paper_count FROM topics t LEFT JOIN paper_topics pt ON pt.topic_id=t.id LEFT JOIN papers p ON p.id=pt.paper_id WHERE t.status='active' GROUP BY t.id",(today(),))


@router.get('/feed/{context}')
def paper_feed(context: Literal['today','backlog'],offset: int=Query(0,ge=0),limit: int=Query(20,ge=1,le=100),exclude: str=Query('',max_length=40000),user=Depends(optional_user)):
    try:
        ids = [int(value) for value in exclude.split(',') if value]
    except ValueError:
        raise HTTPException(400,'论文编号格式不正确')
    return feed(user['id'] if user else None,context,offset,limit,ids)


class Interaction(BaseModel):
    paper_id: int | None = None
    action: Literal['view','skip','like','save','expand','undo','remove_like','remove_save']
    dwell_ms: int = Field(0,ge=0,le=86400000)
    feed_context: Literal['today','backlog','browse','library'] = 'today'
    target_id: int | None = None


@router.post('/interactions')
def interact(body: Interaction,user=Depends(current_user)):
    uid = user['id']
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if body.action=='view':
            if body.dwell_ms<VIEW_DWELL_MS:
                return {'id':None,'recorded':False}
            from zoneinfo import ZoneInfo
            from ..config import settings
            start = datetime.combine(date.fromisoformat(today()),datetime.min.time(),tzinfo=ZoneInfo(settings().tz)).astimezone(timezone.utc).isoformat()
            previous = db.execute("SELECT id FROM interactions WHERE user_id=? AND paper_id=? AND action='view' AND created_at>=? LIMIT 1",(uid,body.paper_id,start)).fetchone()
            if previous:
                stamp=now()
                db.execute('UPDATE user_paper_state SET last_browsed_at=?,updated_at=? WHERE user_id=? AND paper_id=?',
                           (stamp,stamp,uid,body.paper_id))
                return {'id':previous['id'],'recorded':False}
        if body.action=='undo':
            target = db.execute("SELECT * FROM interactions WHERE id=? AND user_id=? AND action IN ('skip','like','save')",(body.target_id,uid)).fetchone()
            if not target:
                raise HTTPException(404,'操作不存在')
            latest = db.execute("SELECT id FROM interactions WHERE user_id=? AND action IN ('skip','like','save','undo','remove_like','remove_save') ORDER BY id DESC LIMIT 1",(uid,)).fetchone()
            elapsed = (datetime.now(timezone.utc)-datetime.fromisoformat(target['created_at'])).total_seconds()
            if elapsed>3 or latest['id']!=target['id']:
                raise HTTPException(409,'撤销窗口已结束')
            if db.execute("SELECT 1 FROM interactions WHERE target_id=? AND action='undo'",(target['id'],)).fetchone():
                raise HTTPException(409,'已撤销')
            state = json.loads(target['previous_state'])
            current_state = db.execute('SELECT last_browsed_at FROM user_paper_state WHERE user_id=? AND paper_id=?',
                                       (uid,target['paper_id'])).fetchone()
            after_view = current_state and current_state['last_browsed_at'] and current_state['last_browsed_at'] > target['created_at']
            view=db.execute("SELECT MAX(created_at) stamp FROM interactions WHERE user_id=? AND paper_id=? AND action='view' AND created_at>=?",(uid,target['paper_id'],target['created_at'])).fetchone()['stamp']
            if after_view or view:
                state.update(seen=1,last_browsed_at=max(current_state['last_browsed_at'] or '',view or ''))
            db.execute('UPDATE user_paper_state SET liked=?,saved=?,seen=?,dismissed=?,last_browsed_at=?,updated_at=? WHERE user_id=? AND paper_id=?',
                       (state['liked'],state['saved'],state['seen'],state.get('dismissed',0),state.get('last_browsed_at'),now(),uid,target['paper_id']))
            latest_profile = db.execute('SELECT id,embedding_parts FROM interest_profile WHERE user_id=? ORDER BY version DESC LIMIT 1',(uid,)).fetchone()
            if target['action'] in ('like','skip') and latest_profile and latest_profile['id']==target['profile_id'] and target['vector_epoch']==vector_store.epoch(db):
                delta = state.get('interest_vector_before')
                restored = restore_part(latest_profile['embedding_parts'],delta) if delta else None
                if restored is not None:
                    db.execute('UPDATE interest_profile SET embedding=?,embedding_parts=? WHERE id=?',
                               (target['embedding_before'],restored,target['profile_id']))
                elif not delta and not json.loads(latest_profile['embedding_parts'] or '[]'):
                    db.execute('UPDATE interest_profile SET embedding=? WHERE id=?',(target['embedding_before'],target['profile_id']))
            event_id = db.execute('INSERT INTO interactions(user_id,paper_id,action,target_id,created_at) VALUES(?,?,?,?,?)',(uid,target['paper_id'],'undo',target['id'],now())).lastrowid
            return {'id':event_id,'paper_id':target['paper_id'],'action':'undo',**social_state(db,target['paper_id'],uid)}
        paper = db.execute('SELECT * FROM papers WHERE id=?',(body.paper_id,)).fetchone()
        if not paper:
            raise HTTPException(404,'论文不存在')
        state = db.execute('SELECT liked,saved,seen,dismissed,last_browsed_at FROM user_paper_state WHERE user_id=? AND paper_id=?',(uid,body.paper_id)).fetchone()
        before = dict(state) if state else {'liked':0,'saved':0,'seen':0,'dismissed':0,'last_browsed_at':None}
        field = {'like':'liked','save':'saved','remove_like':'liked','remove_save':'saved'}.get(body.action)
        if field and before[field]==int(not body.action.startswith('remove_')):
            return {'id':None,'paper_id':body.paper_id,'action':body.action,'recorded':False,**social_state(db,body.paper_id,uid)}
        after = before.copy()
        stamp=now()
        if body.action in ('view','skip','like','save'):
            after['seen']=1
            after['last_browsed_at']=stamp
        if body.action=='skip':after['dismissed']=1
        if body.action=='like': after['liked']=1
        if body.action=='save': after['saved']=1
        if body.action=='remove_like': after['liked']=0
        if body.action=='remove_save': after['saved']=0
        profile = db.execute('SELECT * FROM interest_profile WHERE user_id=? ORDER BY version DESC LIMIT 1',(uid,)).fetchone()
        if profile and body.action in ('like','skip'):
            updated,parts,delta = update_profile(dict(profile),vector_store.get(body.paper_id,db) if paper['embedding'] is not None else None,body.action)
            if delta:before['interest_vector_before']=delta
        event_id = db.execute('INSERT INTO interactions(user_id,paper_id,action,dwell_ms,feed_context,previous_state,embedding_before,profile_id,created_at,view_rule,vector_epoch) VALUES(?,?,?,?,?,?,?,?,?,1,?)',
             (uid,body.paper_id,body.action,body.dwell_ms,body.feed_context,dumps(before),profile['embedding'] if profile else None,profile['id'] if profile else None,stamp,vector_store.epoch(db))).lastrowid
        db.execute('INSERT INTO user_paper_state(user_id,paper_id,liked,saved,seen,dismissed,last_browsed_at,updated_at) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,paper_id) DO UPDATE SET liked=excluded.liked,saved=excluded.saved,seen=excluded.seen,dismissed=excluded.dismissed,last_browsed_at=excluded.last_browsed_at,updated_at=excluded.updated_at',
                   (uid,body.paper_id,after['liked'],after['saved'],after['seen'],after['dismissed'],after['last_browsed_at'],stamp))
        life_extended = extend_life(db,uid,body.paper_id,body.action,now()) if body.action in ('like','save') else False
        if profile and body.action in ('like','skip'):
            db.execute('UPDATE interest_profile SET embedding=?,embedding_parts=? WHERE id=?',(updated,parts,profile['id']))
        return {'id':event_id,'paper_id':body.paper_id,'action':body.action,'life_extended':life_extended,**social_state(db,body.paper_id,uid)}


@router.get('/stats/today')
def daily_stats(user=Depends(current_user)):
    from zoneinfo import ZoneInfo
    from ..config import settings
    start = datetime.combine(date.fromisoformat(today()),datetime.min.time(),tzinfo=ZoneInfo(settings().tz)).astimezone(timezone.utc).isoformat()
    events = rows("SELECT paper_id,action,created_at,view_rule FROM interactions i WHERE user_id=? AND created_at>=? AND NOT EXISTS(SELECT 1 FROM interactions u WHERE u.target_id=i.id AND u.action='undo')",(user['id'],start))
    counts = {'view':0,'skip':0,'like':0,'save':0,'expand':0}
    for e in events:
        if datetime.fromisoformat(e['created_at']).astimezone(ZoneInfo(settings().tz)).date().isoformat()==today() and e['action'] in counts:
            counts[e['action']]+=1
    profile = current(user['id'])
    shown = len({e['paper_id'] for e in events if e['action']=='view' or e['view_rule']==0 and e['action'] in ('skip','like','save')})
    state=one('SELECT COALESCE(SUM(s.liked=1),0) likes,COALESCE(SUM(s.saved=1),0) saves FROM user_paper_state s JOIN papers p ON p.id=s.paper_id WHERE s.user_id=?',(user['id'],))
    counts.update(like=state['likes'],save=state['saves'])
    return {**counts,'shown':shown,
            'profile_summary':profile['content'] if profile else '',
            'profile_change':('您的阅读反馈正在调整推荐排序' if counts['like'] or counts['save'] else '多刷几篇，让推荐了解您的研究兴趣')}


@router.get('/papers/{paper_id}')
def paper_detail(paper_id: int,user=Depends(optional_user)):
    paper = one('SELECT * FROM papers WHERE id=?',(paper_id,))
    if not paper: raise HTTPException(404,'论文不存在')
    ranked = scored_papers(user['id'] if user else None,[paper])
    return ranked[0] if ranked else serialize_paper(paper)


@router.get('/papers/{paper_id}/card')
async def reading_card(paper_id: int,retry: bool=False,regenerate: bool=False,level: Literal['L2','L3']='L2',user=Depends(current_user)):
    paper = one('SELECT * FROM papers WHERE id=?',(paper_id,))
    if not paper: raise HTTPException(404,'论文不存在')
    cached=request_card(paper_id,level=level,retry=retry,regenerate=regenerate,allow_regenerate=bool(user['is_admin']))
    body=card_response(cached)
    return JSONResponse(body,status_code=200 if cached['status']=='ready' else 202)


class CardRequest(BaseModel):
    level: Literal['L2','L3']='L2'
    retry: bool=False
    regenerate: bool=False


@router.post('/papers/{paper_id}/card/stream')
async def reading_card_stream(paper_id: int,body: CardRequest,user=Depends(current_user)):
    if not one('SELECT id FROM papers WHERE id=?',(paper_id,)):
        raise HTTPException(404,'论文不存在')
    request_card(paper_id,level=body.level,retry=body.retry,regenerate=body.regenerate,allow_regenerate=bool(user['is_admin']))
    return StreamingResponse(card_events(paper_id),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})


@router.get('/browse')
def browse(topic_id: int | None=None,category: str | None=None,range: Literal['today','two_days','week','month','all']='week',sort: Literal['score','date']='score',date_basis: Literal['paper','ingested']='paper',year: int | None=Query(None,ge=1900,le=2200),month: str | None=Query(None,pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),query: str=Query('',max_length=200),offset: int=Query(0,ge=0),limit: int=Query(50,ge=1,le=100),user=Depends(optional_user)):
    where,args = [],[]
    date_column='p.ingested_date' if date_basis=='ingested' else 'p.paper_date_sort'
    if date_basis=='paper' and (month or year):where.append("p.paper_date_basis!='ingested'")
    if month:
        month_start=date.fromisoformat(month+'-01')
        month_end=(month_start.replace(day=28)+timedelta(days=4)).replace(day=1)
        where.append(date_column+'>=? AND '+date_column+'<?')
        args.extend((month+'-00',month_end.strftime('%Y-%m')+'-00'))
    elif year:
        where.append(date_column+'>=? AND '+date_column+'<?')
        args.extend((f'{year}-00-00',f'{year+1}-00-00'))
    elif range!='all':
        days = {'today':0,'two_days':1,'week':6,'month':29}[range]
        if date_basis=='paper':where.append("p.paper_date_basis='publication' AND LENGTH(p.paper_date)=10")
        where.append(date_column+'>=? AND '+date_column+'<=?')
        args.append((date.fromisoformat(today())-timedelta(days=days)).isoformat())
        args.append(today())
    if topic_id:
        where.append("p.id IN (SELECT pt.paper_id FROM paper_topics pt JOIN topics t ON t.id=pt.topic_id WHERE t.id=? AND t.status='active')")
        args.append(topic_id)
    if query:
        from ..paper_index import search_clause
        clause,parameters=search_clause(query)
        where.append('('+clause+')');args.extend(parameters)
    if category:
        validate_category_keys([category])
        where.append('p.id IN (SELECT paper_id FROM paper_categories WHERE category_key=?)')
        args.append(category.casefold())
    return browse_page(user['id'] if user else None,' AND '.join(where) if where else '1',args,sort,offset,limit,date_basis)


@router.get('/library')
def library(type: Literal['like','save','history']='save',limit: int=Query(20,ge=1,le=100),cursor: str=Query('',max_length=200),user=Depends(current_user)):
    if type=='history':
        clause='s.user_id=? AND s.seen=1 AND s.last_browsed_at IS NOT NULL'
        args=[user['id']]
        if cursor:
            try:
                stamp,ident=json.loads(cursor)
                datetime.fromisoformat(stamp)
                if not isinstance(ident,int) or isinstance(ident,bool) or not 0<ident<=9223372036854775807:raise ValueError()
            except (ValueError,TypeError):raise HTTPException(400,'浏览记录游标格式不正确')
            clause+=' AND (s.last_browsed_at,s.paper_id)<(?,?)';args.extend([stamp,ident])
        from ..pipeline.score import PAPER_COLUMNS
        papers=rows('SELECT '+PAPER_COLUMNS+',s.last_browsed_at,s.liked,s.saved FROM user_paper_state s INDEXED BY idx_state_browsed JOIN papers p ON p.id=s.paper_id WHERE '+clause+' ORDER BY s.last_browsed_at DESC,s.paper_id DESC LIMIT ?',[*args,limit+1])
        more=len(papers)>limit;papers=papers[:limit]
        ranked={p['id']:p for p in scored_papers(user['id'],papers)}
        items=[]
        for paper in papers:
            item=ranked.get(paper['id']) or serialize_paper(paper)
            item.update(liked=bool(paper['liked']),saved=bool(paper['saved']))
            items.append(item)
        return {'items':items,
                'next_cursor':dumps([papers[-1]['last_browsed_at'],papers[-1]['id']]) if more else None}
    field = 'liked' if type=='like' else 'saved'
    papers = rows(f'SELECT p.* FROM papers p JOIN user_paper_state s ON s.paper_id=p.id WHERE s.user_id=? AND s.{field}=1 ORDER BY s.updated_at DESC',(user['id'],))
    ranked = {p['id']:p for p in scored_papers(user['id'],papers)}
    return {'items':[ranked.get(p['id'],serialize_paper(p)) for p in papers]}


@router.get('/trends')
def trends(background_tasks: BackgroundTasks,window: Literal['day','week','month']='day',user=Depends(current_user)):
    result=trend_data(user['id'],window)
    request_update(user['id'],needed=result['status'] in ('pending','updating'))
    return result


@router.get('/trends/summary')
def interest_trends(background_tasks: BackgroundTasks,user=Depends(optional_user)):
    if not user:
        return {'summary':'','status':'login_required','personalized':False,'created_at':None}
    user_id = user['id'] if user else None
    summary = trend_summary(user_id)
    request_update(user_id,needed=summary['status'] in ('pending','updating'))
    return summary


@router.get('/notifications')
def notifications(unread: bool=False,status: Literal['all','read','unread']='all',type: str=Query('',max_length=80),offset: int=Query(0,ge=0),limit: int=Query(20,ge=1,le=100),user=Depends(current_user)):
    where,args = 'user_id=?',[user['id']]
    state = 'unread' if unread else status
    if state!='all':
        where += ' AND read=?'
        args.append(0 if state=='unread' else 1)
    if type:
        where += ' AND type=?'
        args.append(type)
    with connect() as db:
        total = db.execute('SELECT COUNT(*) n FROM notifications WHERE '+where,args).fetchone()['n']
        count = db.execute('SELECT COUNT(*) n FROM notifications WHERE user_id=? AND read=0',(user['id'],)).fetchone()['n']
        items = [dict(row) for row in db.execute('SELECT * FROM notifications WHERE '+where+' ORDER BY id DESC LIMIT ? OFFSET ?',[*args,limit,offset])]
    return {'items':items,'unread':count,'total':total,'offset':offset,'limit':limit}


@router.get('/notifications/count')
def notification_count(user=Depends(current_user)):
    return {'unread':one('SELECT COUNT(*) n FROM notifications WHERE user_id=? AND read=0',(user['id'],))['n']}


@router.post('/notifications/{notification_id}/read')
def read_notification(notification_id: int,user=Depends(current_user)):
    with connect() as db:
        if not db.execute('UPDATE notifications SET read=1 WHERE id=? AND user_id=?',(notification_id,user['id'])).rowcount:
            raise HTTPException(404,'通知不存在')
    return {'ok':True}


class NotificationSelection(BaseModel):
    ids: list[int] = Field(min_length=1,max_length=100)


class NotificationState(NotificationSelection):
    read: bool


@router.patch('/notifications/batch')
def update_notifications(body: NotificationState,user=Depends(current_user)):
    ids = list(dict.fromkeys(body.ids))
    with connect() as db:
        count = db.execute('UPDATE notifications SET read=? WHERE user_id=? AND id IN ('+','.join('?' for _ in ids)+')',[int(body.read),user['id'],*ids]).rowcount
    return {'updated':count}


@router.delete('/notifications/batch')
def delete_notifications(body: NotificationSelection,user=Depends(current_user)):
    ids = list(dict.fromkeys(body.ids))
    with connect() as db:
        count = db.execute('DELETE FROM notifications WHERE user_id=? AND id IN ('+','.join('?' for _ in ids)+')',[user['id'],*ids]).rowcount
    return {'deleted':count}
