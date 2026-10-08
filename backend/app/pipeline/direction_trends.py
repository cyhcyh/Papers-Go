"""Cached, bounded analyses of the latest complete official arXiv announcement."""
import asyncio
import hashlib
import json
import re
import uuid
import unicodedata
from collections import Counter
from datetime import datetime, timedelta
from .. import prompts
from ..config import now, settings
from ..db import connect, one, rows, dumps
from ..interest.profile import active_entries, current
from ..interest.scope import category_weights
from ..source_catalog import registry
from ..llm import runtime as models
from ..standard_topics import words, catalog
from . import arxiv_daily

_generating=set()
_tasks={}
_owners={}
_PROMPT_KEYS=('trend_report','trend_rewrite')
_FORMAT_REVISION='arxiv-daily-v8'
DAILY_DIRECTION='每日趋势'
FULL_MAX=1200
SHORT_MAX=400
PROGRESS_LABELS={'breakthrough':'突破性进展','substantial':'重要推进','incremental':'阶段性推进','uncertain':'进展程度待判断'}


def _revision(versions):
    return dumps([_FORMAT_REVISION,*[versions.get(k,prompts.DEFAULTS[k].get('version','default-v1')) for k in _PROMPT_KEYS]])


def _current_prompt_revision(db=None):
    found=db.execute("SELECT value FROM app_settings WHERE name='prompt_versions'").fetchone() if db else one("SELECT value FROM app_settings WHERE name='prompt_versions'")
    return _revision(json.loads(found['value']) if found else {})


def _text(value):return value.replace('\r\n','\n').replace('\r','\n').strip()


def valid_text(summary,short,*,daily=False):
    if re.search(r'新增\s*\d+\s*篇|环比|同比|增长率|https?://',summary+'\n'+short):return False
    if daily:
        paragraphs=[p.strip() for p in re.split(r'\n\s*\n',short) if p.strip()]
        return not summary and 0<len(short)<=SHORT_MAX and 1<=len(paragraphs)<=3 and not re.search(r'(?m)^\s*(?:[-*]\s|\d+[.)]\s|#|>)|\$[^$\n]+\$|\\\(|\\\[',short)
    points=re.findall(r'(?m)^\s*\d+\.\s+\*\*',summary)
    return 0<len(summary)<=FULL_MAX and not short and 1<=len(points)<=4


def daily_overview_text(topics,ids,current_ids):
    if not isinstance(topics,list) or not 1<=len(topics)<=3:raise ValueError('每日概览须包含1～3个具体主题')
    names=set();covered=set();parts=[];levels=[]
    for topic in topics:
        if not isinstance(topic,dict):raise ValueError('每日主题结构不正确')
        name=topic.get('name');progress=topic.get('progress');evidence=topic.get('paper_ids')
        if not isinstance(name,str) or not 1<=len(name.strip())<=60 or re.search(r'[\r\n]|\*\*|https?://',name):raise ValueError('每日主题名称不正确')
        name=name.strip();normalized=unicodedata.normalize('NFKC',name).casefold()
        if normalized in names or not isinstance(progress,str) or progress not in PROGRESS_LABELS:raise ValueError('每日主题重复或进展程度不正确')
        if not isinstance(evidence,list) or not evidence or any(type(i)!=int or i not in ids for i in evidence) or len(evidence)!=len(set(evidence)) or not current_ids.intersection(evidence):raise ValueError('每日主题缺少对应的当日证据')
        names.add(normalized);covered.update(evidence);parts.append('**'+name+'**');levels.append(progress)
    if covered!=set(ids):raise ValueError('每日概览证据与所写主题不一致')
    if len(set(levels))==1:
        level=levels[0]
        text='今日研究聚焦'+('、'.join(parts[:-1])+'和'+parts[-1] if len(parts)>1 else parts[0])+'。'
        text+=('这些主题的进展程度尚难判断。' if len(parts)>1 else '这一主题的进展程度尚难判断。') if level=='uncertain' else ('这些主题均有' if len(parts)>1 else '这一主题有')+PROGRESS_LABELS[level]+'。'
    else:
        text='今日研究聚焦'+('、'.join(parts[:-1])+'和'+parts[-1])+'。'
    if not valid_text('',text,daily=True):raise ValueError('每日主题名称无法形成有效概览')
    return text


def _terms(value):
    # Turán/Turan and analogous accented names should match across languages.
    return words(''.join(c for c in unicodedata.normalize('NFKD',value) if not unicodedata.combining(c)))


def interest_queries(profile):
    # Reuse the descriptions already prepared when interests were saved.
    # Reading a trend never requests another translation or embedding.
    return {re.sub(r'\[until:[^]]*\]','',p['text']).strip()[:200]:p.get('query',p['text'])[:800] for p in json.loads(profile.get('embedding_parts') or '[]')} if profile else {}


def profile_context(user_id):
    profile=current(user_id) if user_id else None
    return profile,json.loads(profile['structured']) if profile else {},profile['version'] if profile else 0


def direction_groups(profile,structured):
    sources={s['key']:s for s in registry(True) if s['kind']=='arxiv'}
    topics={t['id']:t for t in rows("SELECT * FROM topics WHERE status='active'")}
    selection=structured.get('category_selection') or {}
    weights=category_weights(structured)
    ids=list(dict.fromkeys(i for key,ids in selection.get('topics',{}).items() if key in sources for i in ids)) if 'category_selection' in structured else structured.get('topic_ids',[])
    selected=[topics[i] for i in ids if i in topics and set(json.loads(topics[i]['category_keys'] or '[]'))&sources.keys()]
    selected.sort(key=lambda t:-max((weights.get(k,0) for k,ids in selection.get('topics',{}).items() if t['id'] in ids),default=0))
    entries=sorted((e for e in active_entries(profile['content'],include_inferred=False) if not e['excluded'] and e['weight']>0),key=lambda e:-e['weight']) if profile else []
    groups=[]
    for entry in entries:
        label=re.sub(r'\[until:[^]]*\]','',entry['text']).strip()
        if not label:continue
        topic=next((t for t in topics.values() if t['name_zh'] in label or t['name_en'].casefold() in label.casefold()),None)
        key=next((k for k,s in sources.items() if len(s['label'])>2 and (s['label'].casefold() in label.casefold() or s['code'].casefold() in label.casefold())),None)
        groups.append((label[:200],key,topic['id'] if topic else None))
    for topic in selected:
        if not any(tid==topic['id'] for _,_,tid in groups):groups.append((topic['name_zh'],None,topic['id']))
    if not groups:
        keys=selection.get('categories',[]) or list(dict.fromkeys(k for k,ids in selection.get('topics',{}).items() if ids))
        if 'category_selection' not in structured and not keys:keys=list(sources)
        groups=[(sources[k]['label'],k,None) for k in sorted(keys,key=lambda k:-weights.get(k,0)) if k in sources]
    # Keep model spend bounded even when the entire taxonomy is selected.
    return list(dict.fromkeys(groups))[:5],topics


def trend_context(user_id):
    profile,structured,_=profile_context(user_id);groups,_=direction_groups(profile,structured)
    selection=structured.get('category_selection')
    if selection is not None:
        selection={'categories':sorted(selection.get('categories',[])), 'topics':{k:sorted(v) for k,v in sorted(selection.get('topics',{}).items()) if v},
                   'weights':category_weights(structured)}
    batch=arxiv_daily.latest()
    queries=interest_queries(profile)
    key=dumps([selection,sorted(structured.get('topic_ids',[])) if selection is None else [],structured.get('source_selection_removed',False),groups,[queries.get(label,label) for label,_,_ in groups]])
    return {'profile':profile,'structured':structured,'batch':batch,'key':'arxiv-daily:'+str(batch['id'] if batch else 0)+':'+hashlib.sha256(key.encode()).hexdigest()[:32]}


def audience(user_id,window='day'):
    return trend_context(user_id)['key']


def period(window='day'):
    batch=arxiv_daily.latest();day=batch['announcement_date'] if batch else None
    return {'from':day,'through':day}


def latest_material(user_id=None):
    batch=arxiv_daily.latest()
    return batch['revision'] if batch else 0


def cached(user_id,window='day'):
    return one('SELECT * FROM direction_trends WHERE audience=?',(audience(user_id),))


_UNSET=object()


def needs_update(user_id,window='day',*,context=None,value=_UNSET):
    context=context or trend_context(user_id)
    if not user_id or not context['batch']:return False
    user=one('SELECT disabled FROM users WHERE id=?',(user_id,))
    if not user or user['disabled']:return False
    key=context['key'];material_id=context['batch']['revision']
    if key in _generating:return False
    if value is _UNSET:value=one('SELECT * FROM direction_trends WHERE audience=?',(key,))
    if not value:return True
    revision=_current_prompt_revision()
    if value['attempted_prompt_revision']==revision and value['attempted_material_id']==material_id and value['attempted_at'] and datetime.fromisoformat(now())-datetime.fromisoformat(value['attempted_at'])<timedelta(minutes=5):return False
    return value['prompt_revision']!=revision or bool(value['error']) or value['material_id']!=material_id


def request_update(user_id,*,needed=None):
    # An indexed, deduplicated request; HTTP never executes fetch/classify/LLM.
    should_refresh=needed if needed is not None else needs_update(user_id)
    if should_refresh:
        with connect() as db:db.execute('INSERT OR IGNORE INTO arxiv_trend_requests VALUES(?,?)',(user_id,now()))


def scoped_papers(user_id,*,fields='p.id,p.classified,p.classification_state',context=None):
    from .score import scope_clause
    context=context or trend_context(user_id);clause,args=scope_clause(context['structured'])
    batch=context['batch']
    if not batch:return []
    return rows('SELECT DISTINCT '+fields+' FROM arxiv_batch_papers b JOIN papers p ON p.id=b.paper_id WHERE b.batch_id=? AND '+clause,[batch['id'],*args])


def topic_distribution(ids,*,minimum=3,limit=6):
    if not ids:return []
    marks=','.join('?' for _ in ids)
    return rows("SELECT t.*,COUNT(DISTINCT pt.paper_id) count FROM paper_topics pt JOIN topics t ON t.id=pt.topic_id WHERE t.status='active' AND pt.paper_id IN ("+marks+") GROUP BY t.id HAVING COUNT(DISTINCT pt.paper_id)>=? ORDER BY count DESC,t.id LIMIT ?",[*ids,minimum,limit])


def coverage(user_id,*,context=None,papers=None):
    context=context or trend_context(user_id);batch=context['batch']
    if papers is None:papers=scoped_papers(user_id,context=context)
    classified=sum(bool(p['classified']) for p in papers)
    awaiting=sum(p['classification_state']=='awaiting_approval' for p in papers)
    return {'announcement_date':batch['announcement_date'] if batch else None,'batch_count':batch['expected_count'] if batch else 0,
            'matched_count':len(papers),'classified_count':classified,'pending_count':len(papers)-classified,
            'awaiting_approval_count':awaiting,'classification_coverage':round(classified/len(papers)*100,1) if papers else 0}


def trend_summary(user_id,window='day',*,context=None,papers=None):
    context=context or trend_context(user_id)
    value=one('SELECT * FROM direction_trends WHERE audience=?',(context['key'],));items=json.loads(value['items_json']) if value else []
    updating=needs_update(user_id,context=context,value=value) or context['key'] in _generating
    if value and value['attempted_at'] and not value['error'] and (value['attempted_prompt_revision']!=value['prompt_revision'] or value['attempted_material_id']!=value['material_id']):
        updating=updating or datetime.fromisoformat(now())-datetime.fromisoformat(value['attempted_at'])<timedelta(minutes=5)
    status='updating' if items and updating else 'ready' if items else 'pending' if updating else 'error' if value and value['error'] else 'empty'
    day=context['batch']['announcement_date'] if context['batch'] else None
    return {'summary':value['summary'] if value else '', 'items':items,'period':{'from':day,'through':day},'window':'day','status':status,
            'personalized':bool(context['profile']), 'created_at':value['created_at'] if value else None,
            'papers':json.loads(value['evidence']) if value else [],'coverage':coverage(user_id,context=context,papers=papers)}


def input_tokens(text):
    # Conservative estimate without an extra tokenizer/model/dependency.
    ascii_count=sum(ord(c)<128 for c in text)
    return (ascii_count+2)//3+(len(text)-ascii_count)*2+64


def model_payload(materials,groups=None):
    """Send complete evidence once, even when several interests share a paper."""
    output=[];papers={}
    for group in materials['groups'] if groups is None else groups:
        item={k:v for k,v in group.items() if k not in ('papers','reference_papers')}
        item['paper_ids']=[p['id'] for p in group['papers']]
        item['reference_ids']=[p['id'] for p in group.get('reference_papers',[])]
        for paper in [*group['papers'],*group.get('reference_papers',[])]:papers[paper['id']]=paper
        output.append(item)
    return {'period':materials['period'],'announcement_date':materials['announcement_date'],
            'reference_period':materials['reference_period'],'groups':output,'papers':list(papers.values())}


def historical_candidates(structured,groups,batch,day):
    from .score import scope_clause
    clause,args=scope_clause(structured);start=(datetime.fromisoformat(day)-timedelta(days=30)).date().isoformat()
    found={}
    # Indexed date/topic/category lookups, capped per interest so a busy field
    # cannot consume every reference slot. This runs only in background generation.
    for _,key,tid in groups:
        extra='';extra_args=[]
        if tid:
            extra=' AND p.id IN (SELECT paper_id FROM paper_topics WHERE topic_id=?)';extra_args=[tid]
        elif key:
            extra=' AND p.id IN (SELECT paper_id FROM paper_categories WHERE category_key=?)';extra_args=[key.casefold()]
        for paper in rows('SELECT p.id,p.title,p.abstract,p.published,p.quality_score,p.brief_json FROM papers p '
                'WHERE p.venue IS NULL AND p.published>=? AND p.published<? AND '+clause+extra+
                ' AND NOT EXISTS(SELECT 1 FROM arxiv_batch_papers b WHERE b.batch_id=? AND b.paper_id=p.id) '
                'ORDER BY p.published DESC,p.id DESC LIMIT 80',[start,day,*args,*extra_args,batch['id']]):
            found[paper['id']]=paper
    return list(found.values())


def research_materials(profile,structured,window='day',vectors=None):
    from .score import scope_clause
    groups,topics=direction_groups(profile,structured);batch=arxiv_daily.latest();bounds=period()
    clause,args=scope_clause(structured)
    papers=rows('SELECT DISTINCT p.id,p.title,p.abstract,p.published,p.quality_score,p.brief_json,p.classified,p.classification_state FROM arxiv_batch_papers b JOIN papers p ON p.id=b.paper_id WHERE b.batch_id=? AND '+clause,[batch['id'],*args]) if batch else []
    historical=historical_candidates(structured,groups,batch,bounds['through']) if batch else []
    memberships={p['id']:set() for p in [*papers,*historical]};categories={p['id']:set() for p in [*papers,*historical]}
    decisions={}
    if batch:
        decisions={r['paper_id']:r['standard_key'] for r in rows('SELECT pc.paper_id,pc.standard_key FROM paper_classifications pc JOIN arxiv_batch_papers b ON b.paper_id=pc.paper_id WHERE b.batch_id=?',(batch['id'],))}
        for r in rows('SELECT pt.paper_id,pt.topic_id FROM paper_topics pt JOIN arxiv_batch_papers b ON b.paper_id=pt.paper_id WHERE b.batch_id=?',(batch['id'],)):
            if r['paper_id'] in memberships:memberships[r['paper_id']].add(r['topic_id'])
        for r in rows('SELECT pc.paper_id,pc.category_key FROM paper_categories pc JOIN arxiv_batch_papers b ON b.paper_id=pc.paper_id WHERE b.batch_id=?',(batch['id'],)):
            if r['paper_id'] in categories:categories[r['paper_id']].add(r['category_key'].casefold())
    if historical:
        ids=[p['id'] for p in historical];marks=','.join('?' for _ in ids)
        decisions.update({r['paper_id']:r['standard_key'] for r in rows('SELECT paper_id,standard_key FROM paper_classifications WHERE paper_id IN ('+marks+')',ids)})
        for r in rows('SELECT paper_id,topic_id FROM paper_topics WHERE paper_id IN ('+marks+')',ids):memberships[r['paper_id']].add(r['topic_id'])
        for r in rows('SELECT paper_id,category_key FROM paper_categories WHERE paper_id IN ('+marks+')',ids):categories[r['paper_id']].add(r['category_key'].casefold())
    paper_terms={p['id']:_terms(p['title']+' '+p['abstract']) for p in [*papers,*historical]}
    week_start=(datetime.fromisoformat(bounds['through'])-timedelta(days=7)).date().isoformat() if batch else ''
    buckets=[];candidates=[];references=[];areas=list(catalog().values());queries=interest_queries(profile)
    for label,key,tid in groups:
        topic=topics.get(tid,{})
        area=next((a for a in areas if a['name_zh'] in label or a['name'].casefold() in label.casefold()),None) if not tid and not key else None
        terms=_terms(queries.get(label,label)+' '+topic.get('name_en','')+' '+(area['name'] if area else ''));required_terms=_terms(label) or terms
        def ranked(pool,reference=False):
            relevant=[]
            for paper in pool:
                if key and key.casefold() not in categories[paper['id']]:continue
                if tid and tid not in memberships[paper['id']]:continue
                tokens=paper_terms[paper['id']];lexical=len(terms&tokens)
                if not key and not tid:
                    if area and paper['id'] in decisions:
                        if decisions[paper['id']]!=area['key']:continue
                    elif len(required_terms&tokens)<min(2,len(required_terms)) or not required_terms:continue
                relevant.append((lexical+(paper['quality_score'] or 50)/200,paper))
            relevant.sort(key=lambda v:(-v[0],-(v[1]['published']>=week_start) if reference else 0,v[1]['id']))
            return [p for _,p in relevant]
        relevant=ranked(papers)
        group={'direction':label,'papers':[],'reference_papers':[],'matched_count':len(relevant)}
        if topic:group.update(topic=topic['name_en'],description=topic.get('description','')[:300])
        buckets.append(group);candidates.append(relevant[:12]);references.append(ranked(historical,True)[:4])
    # Reuse the prioritized interest evidence in this same model request. Broad
    # category popularity is only a fallback when no specific interest was set.
    focus=[]
    if any(tid or not key for _,key,tid in groups):
        for group,available,(_,key,tid) in zip(buckets,candidates,groups):
            if (key and not tid) or not available:continue
            name=topics[tid]['name_zh'] if tid else group['direction']
            if any(item['name']==name for item in focus):continue
            focus.append({'name':name,'count':group['matched_count'],'available':available[:12]})
            if len(focus)==3:break
    else:
        counts=Counter(tid for p in papers for tid in memberships[p['id']] if tid in topics)
        weights={k.casefold():v for k,v in category_weights(structured).items()}
        priorities={}
        for pid,tids in ((p['id'],memberships[p['id']]) for p in papers):
            weight=max((weights.get(k,0) for k in categories[pid]),default=0)
            for tid in tids:priorities[tid]=max(priorities.get(tid,0),weight)
        selected=sorted((tid for tid,n in counts.items() if n>=1),key=lambda tid:(-priorities.get(tid,0),-counts[tid],tid))[:3]
        ranked=sorted(papers,key=lambda p:(-(p['quality_score'] or 50),p['id']))
        focus=[{'name':topics[tid]['name_zh'],'count':counts[tid],'available':[p for p in ranked if tid in memberships[p['id']]][:12]} for tid in selected]
    chosen=[]
    for index in range(12):
        for available in (item['available'] for item in focus):
            if index<len(available) and available[index] not in chosen:chosen.append(available[index])
    overview={'direction':DAILY_DIRECTION,'kind':'daily_overview','papers':[],'reference_papers':[],
              'matched_count':len(papers),'focus_topics':[{'name':item['name'],'count':item['count']} for item in focus],
              'coverage_complete':all(p['classified'] and p['classification_state']!='awaiting_approval' for p in papers)}
    buckets.insert(0,overview);candidates.insert(0,chosen[:12]);references.insert(0,[])
    materials={'period':bounds,'announcement_date':bounds['through'],'groups':buckets,
               'reference_period':{'from':(datetime.fromisoformat(bounds['through'])-timedelta(days=30)).date().isoformat() if batch else None,
                                   'through':(datetime.fromisoformat(bounds['through'])-timedelta(days=1)).date().isoformat() if batch else None}}
    # Reserve part of the per-run total for a single formatting correction.
    budget=settings().trend_input_tokens-2000
    prompt_cost=input_tokens(prompts.get('trend_report'))
    def fits():return prompt_cost+input_tokens(dumps(model_payload(materials)))<=budget-256
    def add(group,p,reference=False):
        item={k:p[k] for k in ('id','title','published','abstract')}
        item['evidence_kind']='reference' if reference else 'current'
        if p.get('brief_json'):
            try:brief=json.loads(p['brief_json'])
            except ValueError:brief=None
            if brief and len(dumps(brief))<=700:item['brief']=brief
        target=group['reference_papers'] if reference else group['papers']
        target.append(item)
        if not fits():target.pop()
    # One current result per interest, then one comparison, before adding breadth.
    # Whole abstracts are admitted or skipped; scientific conclusions are never cut.
    for group,available in zip(buckets,candidates):
        if available:add(group,available[0])
    for group,available in zip(buckets,references):
        if group['papers'] and available:add(group,available[0],True)
    for index in range(1,12):
        for group,available in zip(buckets,candidates):
            if index>=len(available):continue
            add(group,available[index])
    for index in range(1,4):
        for group,available in zip(buckets,references):
            if group['papers'] and index<len(available):add(group,available[index],True)
    sampled={p['id'] for p in overview['papers']}
    overview['focus_topics']=[{'name':item['name'],'count':item['count'],'paper_ids':[p['id'] for p in item['available'] if p['id'] in sampled]} for item in focus
                              if any(p['id'] in sampled for p in item['available'])]
    focused_ids={pid for item in overview['focus_topics'] for pid in item['paper_ids']}
    overview['papers']=[p for p in overview['papers'] if p['id'] in focused_ids]
    for group in buckets:group['sampled_count']=len(group['papers'])
    materials['papers']=model_payload(materials)['papers']
    return materials


async def cancel_user_generation(user_id):
    targets=[_tasks[key] for key,owner in list(_owners.items()) if owner==user_id]
    for task in targets:task.cancel()
    if targets:await asyncio.gather(*targets,return_exceptions=True)


@models.model_task
async def _generate(user_id,window='day',force=False):
    key=audience(user_id);profile,structured,version=profile_context(user_id)
    user=one('SELECT disabled,auth_epoch FROM users WHERE id=?',(user_id,))
    if not user or user['disabled'] or not arxiv_daily.latest():return False
    epoch=user['auth_epoch'];bounds=period();attempted=now();material_id=latest_material()
    revision=_revision({k:prompts.revision(k) for k in _PROMPT_KEYS});claim=uuid.uuid4().hex
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        pending=db.execute('SELECT * FROM direction_trends WHERE audience=?',(key,)).fetchone()
        if not force and pending and pending['attempted_prompt_revision']==revision and pending['attempted_material_id']==material_id and pending['attempted_at'] and datetime.fromisoformat(attempted)-datetime.fromisoformat(pending['attempted_at'])<timedelta(minutes=5):return False
        db.execute('INSERT INTO direction_trends(audience,profile_version,period_start,period_end,attempted_at,attempted_prompt_revision,attempted_material_id,attempt_token) VALUES(?,0,?,?,?,?,?,?) ON CONFLICT(audience) DO UPDATE SET attempted_at=excluded.attempted_at,attempted_prompt_revision=excluded.attempted_prompt_revision,attempted_material_id=excluded.attempted_material_id,attempt_token=excluded.attempt_token,error=NULL',
                   (key,bounds['from'],bounds['through'],attempted,revision,material_id,claim))
    try:
        materials=await asyncio.to_thread(research_materials,profile,structured)
        groups=materials['groups'];pending=[g for g in groups if g['papers']];results={};evidence=[]
        if pending:
            schema={'type':'object','properties':{'items':{'type':'array','items':{'type':'object','properties':{
                'direction':{'enum':[g['direction'] for g in pending]},'summary':{'type':'string'},'short_summary':{'type':'string'},
                'paper_ids':{'type':'array','items':{'type':'integer'}},'focus_directions':{'type':'array','items':{'type':'string'}},'insufficient':{'type':'boolean'},
                'daily_topics':{'type':'array','maxItems':3,'items':{'type':'object','properties':{
                    'name':{'type':'string','minLength':1,'maxLength':60},'progress':{'enum':list(PROGRESS_LABELS)},
                    'paper_ids':{'type':'array','items':{'type':'integer'},'minItems':1,'maxItems':8}},
                    'required':['name','progress','paper_ids'],'additionalProperties':False}}},
                'required':['direction','summary','short_summary','paper_ids','focus_directions','daily_topics','insufficient'],'additionalProperties':False}}},'required':['items'],'additionalProperties':False}
            payload=model_payload(materials,pending)
            messages=[{'role':'system','content':prompts.get('trend_report')},{'role':'user','content':dumps(payload)}]
            used_tokens=sum(input_tokens(m['content']) for m in messages)
            if used_tokens>settings().trend_input_tokens:raise ValueError('趋势输入超过总预算')
            result=await models.complete('trend_report',messages,json_mode=True,schema=schema)
            seen=set();drafts={};repairs=[]
            for item in result.get('items',[]):
                label=item.get('direction');group=next((g for g in pending if g['direction']==label),None)
                if not group or label in seen:raise ValueError('方向名称不正确或重复')
                seen.add(label)
                if item.get('insufficient'):continue
                allowed={p['id']:p for p in [*group['papers'],*group['reference_papers']]};ids=item.get('paper_ids',[])
                current_ids={p['id'] for p in group['papers']}
                if not isinstance(ids,list) or not 1<=len(ids)<=8 or any(type(i)!=int or i not in allowed for i in ids) or len(set(ids))!=len(ids) or not current_ids.intersection(ids):
                    raise ValueError('趋势证据须包含本方向的当日论文，且只能引用所给材料')
                summary=_text(item.get('summary',''));short=_text(item.get('short_summary',''))
                daily=group.get('kind')=='daily_overview'
                focus_names=[item['name'] for item in group['focus_topics']] if daily else None
                selected=item.get('focus_directions')
                if daily:
                    if not isinstance(selected,list) or not 1<=len(selected)<=3 or any(not isinstance(name,str) or name not in focus_names for name in selected) or len(set(selected))!=len(selected) or selected!=[name for name in focus_names if name in selected]:
                        raise ValueError('每日趋势涉及的兴趣条目不正确或顺序不正确')
                    covered=set()
                    for focus in group['focus_topics']:
                        if focus['name'] not in selected:continue
                        if not set(focus['paper_ids']).intersection(ids):raise ValueError('每日趋势缺少所写主题的当日证据')
                        covered.update(focus['paper_ids'])
                    if not set(ids)<=covered:raise ValueError('每日趋势引用了所选兴趣之外的证据')
                    summary='';short=daily_overview_text(item.get('daily_topics'),ids,current_ids)
                elif selected!=[] or item.get('daily_topics')!=[]:raise ValueError('方向分析不应填写每日兴趣条目或主题概览')
                drafts[label]={'summary':summary,'short_summary':short,'ids':ids}
                if not valid_text(summary,short,daily=daily):
                    repairs.append({'direction':label,'kind':group.get('kind','direction'),'focus_topics':group.get('focus_topics',[]),'summary':summary,'short_summary':short})
            if seen!={g['direction'] for g in pending}:raise ValueError('模型缺少部分方向的分析')
            if repairs:
                # At most one formatting call for the whole report, not one per
                # direction. Never cut scientific conditions to fit this budget.
                rewrite_messages=[{'role':'system','content':prompts.get('trend_rewrite')},{'role':'user','content':dumps({'items':repairs})}]
                used_tokens+=sum(input_tokens(m['content']) for m in rewrite_messages)
                if used_tokens>settings().trend_input_tokens:raise ValueError('趋势格式修正超过本轮总输入预算')
                rewritten=await models.complete('trend_report',rewrite_messages,json_mode=True)
                repaired=set();expected={i['direction'] for i in repairs}
                for item in rewritten.get('items',[]):
                    label=item.get('direction')
                    if label not in expected or label in repaired:raise ValueError('趋势改写方向不正确或重复')
                    repaired.add(label);drafts[label].update(summary=_text(item.get('summary','')),short_summary=_text(item.get('short_summary','')))
                if repaired!=expected:raise ValueError('趋势改写缺少部分方向')
            for group in pending:
                label=group['direction']
                if label not in drafts:continue
                draft=drafts[label];summary=draft['summary'];short=draft['short_summary'];ids=draft['ids']
                daily=group.get('kind')=='daily_overview'
                if not valid_text(summary,short,daily=daily):raise ValueError('方向解读的长度、分点或主题格式不正确')
                current_ids={p['id'] for p in group['papers']}
                allowed={p['id']:p for p in [*group['papers'],*group['reference_papers']]}
                results[label]={'direction':label,'status':'ready','summary':summary,'short_summary':short}
                evidence.extend({k:allowed[i][k] for k in ('id','title','published')}|{'direction':label,'historical':i not in current_ids} for i in ids)
        for group in groups:
            label=group['direction']
            if label not in results:results[label]={'direction':label,'status':'insufficient','summary':'本期相关证据不足，暂时无法形成可靠的研究分析。','short_summary':'本期相关证据不足。'}
            results[label].update(matched_count=group['matched_count'],sampled_count=group['sampled_count'],reference_count=len(group['reference_papers']),kind=group.get('kind','direction'))
        with connect() as db:
            db.execute('BEGIN IMMEDIATE')
            account=db.execute('SELECT disabled,auth_epoch FROM users WHERE id=?',(user_id,)).fetchone()
            latest_profile=db.execute('SELECT version FROM interest_profile WHERE user_id=? ORDER BY version DESC LIMIT 1',(user_id,)).fetchone()
            if not account or account['disabled'] or account['auth_epoch']!=epoch or (latest_profile['version'] if latest_profile else 0)!=version:return False
            if revision!=_current_prompt_revision(db) or key!=audience(user_id) or material_id!=latest_material():return False
            items=[results[g['direction']] for g in groups];summary=results[DAILY_DIRECTION]['short_summary'] if results[DAILY_DIRECTION]['status']=='ready' else ''
            return bool(db.execute('UPDATE direction_trends SET summary=?,items_json=?,evidence=?,created_at=?,error=NULL,prompt_revision=?,material_id=? WHERE audience=? AND attempt_token=?',
                                  (summary,dumps(items),dumps(evidence),now(),revision,material_id,key,claim)).rowcount)
    except asyncio.CancelledError:
        with connect() as db:db.execute('UPDATE direction_trends SET attempted_at=NULL WHERE audience=? AND attempt_token=?',(key,claim))
        raise
    except Exception as error:
        with connect() as db:db.execute('UPDATE direction_trends SET error=? WHERE audience=? AND attempt_token=?',(models.safe_error(error)[:500],key,claim))
        return False


async def _guarded_generate(user_id,window,force):
    account=one('SELECT disabled,auth_epoch FROM users WHERE id=?',(user_id,))
    if not account or account['disabled']:return False
    task=asyncio.create_task(_generate(user_id,window,force))
    try:
        while True:
            done,_=await asyncio.wait({task},timeout=2)
            if done:return await task
            latest=one('SELECT disabled,auth_epoch FROM users WHERE id=?',(user_id,))
            if not latest or latest['disabled'] or latest['auth_epoch']!=account['auth_epoch']:return False
    finally:
        if not task.done():task.cancel()
        await asyncio.gather(task,return_exceptions=True)


async def ensure_direction_trend(user_id,force=False,window='day'):
    key=audience(user_id)
    if key in _generating or not user_id or not force and not needs_update(user_id):return False
    _generating.add(key)
    task=asyncio.create_task(_guarded_generate(user_id,window,force));_tasks[key]=task;_owners[key]=user_id
    try:return await task
    finally:_generating.discard(key);_tasks.pop(key,None);_owners.pop(key,None)
