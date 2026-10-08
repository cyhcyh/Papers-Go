"""Incremental, bounded research alerts; expensive reading is an explicit user action."""
import asyncio
import json
import math
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from datetime import date, timedelta

import httpx

from ..db import connect, rows, one, execute, dumps, unpack
from ..config import settings, today, now
from ..logs import event

USER_BATCH = 64
_AUTHOR_ID = re.compile(r'(?:https://openalex.org/)?(A\d+)',re.I)
_COMPARISON = re.compile(r'超越|超过|优于|相比|对比|比较|\b(?:outperform\w*|surpass\w*|beat\w*|compar(?:e[ds]?|ison)\s+(?:to|with|against))\b',re.I)


def folded(value):
    return ' '.join(unicodedata.normalize('NFKC',value or '').casefold().split())


def author_name(value):
    value=unicodedata.normalize('NFKD',value or '').casefold()
    return ' '.join(''.join(c if c.isalnum() else ' ' for c in value if not unicodedata.combining(c)).split())


def keyword_pattern(value):
    value=folded(value)
    if not value:return None
    phrase=r'[\s\-]+'.join(re.escape(part) for part in value.split())
    left=r'(?<![a-z0-9_])' if value[0].isascii() and value[0].isalnum() else ''
    right=r'(?![a-z0-9_])' if value[-1].isascii() and value[-1].isalnum() else ''
    return re.compile(left+phrase+right)


def unit_vector(blob):
    values=unpack(blob)
    norm=math.sqrt(sum(v*v for v in values))
    return [v/norm for v in values] if norm else []


def bind_watch(db, watch):
    """Bind exact topic/author identities using local data only; retain display text."""
    result=dict(watch)
    if watch['type']=='topic':
        topic=None
        if watch.get('topic_key'):
            topic=db.execute("SELECT id,standard_key FROM topics WHERE status='active' AND standard_key=?",(watch['topic_key'],)).fetchone()
        elif watch.get('topic_id'):
            topic=db.execute("SELECT id,standard_key FROM topics WHERE status='active' AND id=?",(watch['topic_id'],)).fetchone()
        else:
            value=watch['value'].strip()
            found=db.execute("SELECT id,standard_key FROM topics WHERE status='active' AND (id=? OR name_zh=? COLLATE NOCASE OR name_en=? COLLATE NOCASE OR standard_key=?) LIMIT 2",(int(value) if value.isdecimal() else -1,value,value,value)).fetchall()
            if len(found)==1:topic=found[0]
        result.update(topic_id=topic['id'] if topic else None,topic_key=topic['standard_key'] if topic else watch.get('topic_key'))
    elif watch['type']=='author' and not watch.get('author_id'):
        explicit=_AUTHOR_ID.fullmatch(watch['value'].strip())
        if explicit:result['author_id']=explicit[1].upper()
        else:
            # Two distinct IDs mean the name is ambiguous. Do not guess an identity.
            matches=db.execute('SELECT author_id FROM author_metrics WHERE name=? COLLATE NOCASE UNION SELECT author_id FROM paper_author_links WHERE name=? COLLATE NOCASE AND confidence>=.98 LIMIT 2',(watch['value'].strip(),watch['value'].strip())).fetchall()
            if len(matches)==1:result['author_id']=matches[0]['author_id']
    if any(result.get(k)!=watch.get(k) for k in ('topic_id','topic_key','author_id')):
        db.execute('UPDATE watches SET topic_id=?,topic_key=?,author_id=? WHERE id=? AND type=? AND value=? AND active=1',
                   (result.get('topic_id'),result.get('topic_key'),result.get('author_id'),watch['id'],watch['type'],watch['value']))
    result['pattern']=keyword_pattern(watch['value'])
    result['name']=author_name(watch['value'])
    return result


def notify(user_id, kind, title, body, paper_id, key):
    with connect() as db:
        db.execute('INSERT OR IGNORE INTO notifications(user_id,type,title,body,paper_id,dedupe_key,created_at) SELECT id,?,?,?,?,?,? FROM users WHERE id=? AND disabled=0 AND notifications_enabled=1',
                   (kind,title,body,paper_id,key,now(),user_id))


def _contexts(db, cache, *, after=0, user_id=None):
    condition='u.id=?' if user_id is not None else 'u.id>?'
    found=db.execute('''SELECT u.id,u.auth_epoch,COALESCE(s.revision,0) alert_revision,p.content,p.embedding
        FROM users u LEFT JOIN alert_user_state s ON s.user_id=u.id
        LEFT JOIN interest_profile p ON p.id=(SELECT id FROM interest_profile WHERE user_id=u.id ORDER BY version DESC LIMIT 1)
        WHERE u.disabled=0 AND u.notifications_enabled=1 AND '''+condition+'''
        AND (EXISTS(SELECT 1 FROM watches WHERE user_id=u.id AND active=1)
          OR EXISTS(SELECT 1 FROM user_paper_state WHERE user_id=u.id AND saved=1)
          OR (INSTR(p.content,'在研方向')>0 AND p.embedding IS NOT NULL))
        ORDER BY u.id LIMIT ?''',(user_id if user_id is not None else after,USER_BATCH+1)).fetchall()
    more=len(found)>USER_BATCH;result=[]
    for row in found[:USER_BATCH]:
        key=(row['id'],row['alert_revision'],row['auth_epoch'])
        value=cache.get(key)
        if value is None:
            watches=[bind_watch(db,dict(w)) for w in db.execute('SELECT * FROM watches WHERE user_id=? AND active=1',(row['id'],)).fetchall()]
            baselines=[]
            for paper in db.execute('SELECT p.id,p.title,p.arxiv_id FROM papers p JOIN user_paper_state s ON s.paper_id=p.id WHERE s.user_id=? AND s.saved=1',(row['id'],)):
                alias=re.split(r'[:：]',paper['title'])[0].strip()
                titles=[paper['title']]
                if len(alias)>=5 and len(alias.split())>=2 and alias!=paper['title']:titles.append(alias)
                baselines.append({'id':paper['id'],'patterns':[keyword_pattern(v) for v in titles+([paper['arxiv_id']] if paper['arxiv_id'] else [])]})
            value={**dict(row),'watches':watches,'baselines':baselines,'vector':unit_vector(row['embedding']) if row['content'] and '在研方向' in row['content'] else []}
            cache[key]=value
            if len(cache)>256:cache.popitem(last=False)
        result.append(value)
    return result,more


def _materials(db, identifiers, cache):
    placeholders=','.join('?' for _ in identifiers)
    papers=[dict(p) for p in db.execute('''SELECT p.id,p.ingested_date,
        COALESCE(s.revision,0) alert_revision FROM papers p
        LEFT JOIN alert_paper_state s ON s.paper_id=p.id
        WHERE p.id IN ('''+placeholders+')',identifiers)]
    missing=[p['id'] for p in papers if (p['id'],p['alert_revision']) not in cache]
    topics={};authors={}
    if missing:
        marks=','.join('?' for _ in missing)
        content={p['id']:dict(p) for p in db.execute('''SELECT p.id,p.title,p.abstract,p.authors,p.embedding,c.card_json
            FROM papers p LEFT JOIN reading_cards c ON c.paper_id=p.id AND c.status='ready'
            WHERE p.id IN ('''+marks+')',missing)}
        from ..vector_store import hydrate
        hydrate(list(content.values()),db=db)
        for row in db.execute("SELECT pt.paper_id,pt.topic_id FROM paper_topics pt JOIN topics t ON t.id=pt.topic_id WHERE t.status='active' AND pt.paper_id IN ("+marks+')',missing):
            topics.setdefault(row['paper_id'],set()).add(row['topic_id'])
        for row in db.execute('SELECT paper_id,author_id,name FROM paper_author_links WHERE confidence>=.98 AND paper_id IN ('+marks+')',missing):
            authors.setdefault(row['paper_id'],{}).setdefault(author_name(row['name']),set()).add(row['author_id'])
    result=[]
    for paper in papers:
        key=(paper['id'],paper['alert_revision']);material=cache.get(key)
        if material is None:
            if paper['id'] not in content:continue
            paper.update(content[paper['id']])
            claims=json.loads(paper['card_json'] or '{}').get('key_results',[])
            verified=[c for c in claims if c.get('verified')]
            names=authors.get(paper['id'],{})
            material={**paper,'body':folded(paper['title']+' '+paper['abstract']+' '+dumps(claims)),
                      'verified':verified,'benchmarks':folded(dumps(verified)),
                      'names':{author_name(a) for a in json.loads(paper['authors'] or '[]')},
                      'author_names':names,'author_ids':{i for ids in names.values() for i in ids},
                      'topics':topics.get(paper['id'],set()),'vector':unit_vector(paper['embedding'])}
            cache[key]=material
        cache.move_to_end(key);result.append(material)
    while len(cache)>500:cache.popitem(last=False)
    return result


def _author_hit(watch, paper):
    identity=watch.get('author_id')
    if identity and identity in paper['author_ids']:return True
    if _AUTHOR_ID.fullmatch(watch['value'].strip()):return False
    if identity and watch['name'] in paper['author_names']:
        return identity in paper['author_names'][watch['name']]
    return watch['name'] in paper['names']


def _required_materials(db,identifiers,cache,users):
    if not users:return []
    # Baseline comparisons and benchmark watches require an existing reading result.
    # Do not load thousands of abstracts/vectors when none of these users need them.
    if not any(u['vector'] or any(w['type']!='benchmark' for w in u['watches']) for u in users):
        identifiers=[r['paper_id'] for r in db.execute("SELECT paper_id FROM reading_cards WHERE status='ready' AND paper_id IN ("+','.join('?' for _ in identifiers)+')',identifiers)]
    return _materials(db,identifiers,cache) if identifiers else []


def _notifications(papers, users, stop):
    for user in users:
        for paper in papers:
            if stop.is_set():return
            for watch in user['watches']:
                if watch['type']=='topic':hit=watch.get('topic_id') in paper['topics']
                elif watch['type']=='author':hit=_author_hit(watch,paper)
                else:hit=bool(watch['pattern'] and watch['pattern'].search(paper['benchmarks'] if watch['type']=='benchmark' else paper['body']))
                if hit:yield user,paper,('watch_hit','监视命中：'+watch['value'],paper['title'],f"watch:{watch['id']}:{paper['id']}")
            for claim in paper['verified']:
                text=folded(claim['claim'])
                if not _COMPARISON.search(text):continue
                for baseline in user['baselines']:
                    if baseline['id']!=paper['id'] and any(pattern.search(text) for pattern in baseline['patterns'] if pattern):
                        # Quoted evidence establishes that a comparison is reported, not that experiments were reproduced.
                        yield user,paper,('baseline_beaten','收藏论文出现相关比较',claim['claim'],f"baseline:{paper['id']}:{baseline['id']}")
            left,right=user['vector'],paper['vector']
            if left and right and len(left)==len(right) and sum(a*b for a,b in zip(left,right))>settings().collision_threshold:
                yield user,paper,('collision','与您的在研方向高度相关',paper['title'],f"collision:{paper['id']}")


def _write_notifications(proposals,stop):
    batch=[];added=0
    def flush():
        if stop.is_set():return 0
        count=0
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for user,paper,(kind,title,body,key) in batch:
                if stop.is_set():break
                count+=db.execute('''INSERT OR IGNORE INTO notifications(user_id,type,title,body,paper_id,dedupe_key,created_at)
                    SELECT u.id,?,?,?,?,?,? FROM users u JOIN papers p ON p.id=?
                    WHERE u.id=? AND u.disabled=0 AND u.notifications_enabled=1 AND u.auth_epoch=?
                    AND COALESCE((SELECT revision FROM alert_user_state WHERE user_id=u.id),0)=?
                    AND COALESCE((SELECT revision FROM alert_paper_state WHERE paper_id=p.id),0)=?
                    AND NOT EXISTS(SELECT 1 FROM notifications WHERE user_id=u.id AND dedupe_key=?)''',
                    (kind,title,body,paper['id'],key,now(),paper['id'],user['id'],user['auth_epoch'],user['alert_revision'],paper['alert_revision'],key)).rowcount
        return count
    for proposal in proposals:
        batch.append(proposal)
        if len(batch)==100:
            added+=flush();batch.clear()
            if stop.is_set():break
    if batch:added+=flush()
    return added


def _ack_papers(papers,users,next_user,more,stop):
    if stop.is_set():return
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        for user in users:
            current=db.execute('SELECT u.auth_epoch,COALESCE(s.revision,0) revision FROM users u LEFT JOIN alert_user_state s ON s.user_id=u.id WHERE u.id=?',(user['id'],)).fetchone()
            if current and (current['auth_epoch']!=user['auth_epoch'] or current['revision']!=user['alert_revision']):return
        if not stop.is_set():
            db.executemany('UPDATE alert_paper_state SET pending=?,cursor_user=? WHERE paper_id=? AND revision=?',
                           [(int(more),next_user if more else 0,p['id'],p['alert_revision']) for p in papers])


def evaluate_alerts(stop=None):
    stop=stop or threading.Event();cfg=settings();started=time.monotonic();deadline=started+cfg.alert_seconds
    materials=OrderedDict();contexts=OrderedDict();processed=set();matched=0;personal=True;progress_at=started
    cutoff=(date.fromisoformat(today())-timedelta(days=1)).isoformat()
    while not stop.is_set() and time.monotonic()<deadline:
        with connect() as db:
            db.execute('BEGIN')
            queued_user=db.execute('SELECT * FROM alert_user_state WHERE pending=1 ORDER BY user_id LIMIT 1').fetchone() if personal else None
            queued_papers=db.execute('SELECT * FROM alert_paper_state WHERE pending=1 ORDER BY paper_id LIMIT ?',(cfg.alert_batch_size,)).fetchall() if not queued_user else []
            if not queued_user and not queued_papers:
                queued_user=db.execute('SELECT * FROM alert_user_state WHERE pending=1 ORDER BY user_id LIMIT 1').fetchone()
            if queued_user:
                state=dict(queued_user);users,_=_contexts(db,contexts,user_id=state['user_id'])
                window=state['window_start'] or cutoff
                checkpoint=[dict(r) for r in db.execute('SELECT id,ingested_date FROM papers INDEXED BY idx_alerts_recent WHERE ingested_date>=? AND (ingested_date,id)>(?,?) ORDER BY ingested_date,id LIMIT ?',
                                                        (window,state['cursor_date'],state['cursor_id'],cfg.alert_batch_size))] if users else []
                identifiers=[r['id'] for r in checkpoint]
                papers=_required_materials(db,identifiers,materials,users) if identifiers else []
                more=len(identifiers)==cfg.alert_batch_size
            elif queued_papers:
                cursor=queued_papers[0]['cursor_user'];identifiers=[p['paper_id'] for p in queued_papers if p['cursor_user']==cursor]
                checkpoint=[{'id':p['paper_id'],'alert_revision':p['revision']} for p in queued_papers if p['cursor_user']==cursor]
                users,more=_contexts(db,contexts,after=cursor);papers=_required_materials(db,identifiers,materials,users)
            else:break
        if stop.is_set():break
        matched+=_write_notifications(_notifications(papers,users,stop),stop)
        if queued_user:
            if not stop.is_set():
                last=max(checkpoint,key=lambda p:(p['ingested_date'],p['id'])) if checkpoint else None
                with connect() as db:
                    db.execute('UPDATE alert_user_state SET pending=?,cursor_date=?,cursor_id=?,window_start=? WHERE user_id=? AND revision=?',
                               (int(more),last['ingested_date'] if last else state['cursor_date'],last['id'] if last else state['cursor_id'],window,state['user_id'],state['revision']))
        else:
            # A material update can reset a paper's user cursor during loading.
            # Acknowledge the queue revision we selected, rather than the later material revision.
            _ack_papers(checkpoint,users,users[-1]['id'] if users else cursor,more,stop)
        processed.update(p['id'] for p in checkpoint)
        if time.monotonic()-progress_at>=1:
            execute('UPDATE source_status SET progress=? WHERE name=\'alert_eval\'',(dumps({'processed':len(processed)}),))
            progress_at=time.monotonic()
        personal=not personal
        # Small pauses keep a large catch-up from continuously occupying disk/CPU.
        if stop.wait(.02):break
    if not stop.is_set():execute('UPDATE source_status SET progress=? WHERE name=\'alert_eval\'',(dumps({'processed':len(processed)}),))
    pending=one('SELECT COUNT(*) n FROM alert_paper_state WHERE pending=1')['n']
    pending_users=one('SELECT COUNT(*) n FROM alert_user_state WHERE pending=1')['n']
    event('task','预警评估完成' if not stop.is_set() else '预警评估停止并保留进度',job='alert_eval',processed=len(processed),matched=matched,pending_papers=pending,pending_users=pending_users,seconds=round(time.monotonic()-started,2))
    return len(processed)


async def push_telegram():
    token=settings().telegram_bot_token
    if not token:return
    async with httpx.AsyncClient(timeout=15) as client:
        for n in rows('SELECT n.*,u.telegram_chat_id FROM notifications n JOIN users u ON u.id=n.user_id WHERE n.pushed=0 AND u.disabled=0 AND u.telegram_chat_id IS NOT NULL AND u.telegram_enabled=1 AND u.notifications_enabled=1 LIMIT 100'):
            try:
                response=await client.post(f'https://api.telegram.org/bot{token}/sendMessage',json={'chat_id':n['telegram_chat_id'],'text':(n['title']+'\n'+(n['body'] or ''))[:4000]})
                response.raise_for_status()
                if response.json().get('ok'):execute('UPDATE notifications SET pushed=1 WHERE id=?',(n['id'],))
            except (httpx.HTTPError,ValueError):pass


async def alert_eval():
    stop=threading.Event()
    try:
        completed=await asyncio.to_thread(evaluate_alerts,stop)
        await push_telegram()
        return completed
    except asyncio.CancelledError:
        stop.set()
        raise
