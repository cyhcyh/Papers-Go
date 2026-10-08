import json
import math
import random
import time
import threading
from collections import OrderedDict
import re
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from ..config import settings, today
from ..db import rows, one, unpack, dumps, connect
from ..interest.profile import current, active_entries
from ..interest.vectors import active_vectors, best_match, quotas
from ..catalog import matches, source_label
from ..interest.scope import in_scope, matched_keys, has_selection, category_weights
from ..taxonomy import paper_keys
from .quality import quality_score, community_score, has_content_score, bounded, NEUTRAL
from ..vector_search import nearest_ids
from .. import vector_store
from .. import recommendation_settings
from .. import browsing

PAPER_COLUMNS = 'p.id,p.arxiv_id,p.arxiv_version,p.title,p.abstract,p.authors,p.categories,p.primary_category,p.venue,p.venue_year,p.venue_rank,p.published,p.paper_date,p.paper_date_basis,p.paper_date_sort,p.ingested_date,p.created_at,p.abs_url,p.pdf_url,p.quality_score,p.base_quality_score,p.author_impact,p.author_impact_known,p.hf_upvotes,p.github_stars,p.tldr,p.brief_json,p.embedding,p.classified,p.scored,p.skeleton,p.like_count,p.save_count,p.expires_at'


def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0
    divisor = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    return sum(x*y for x, y in zip(a, b))/divisor if divisor else 0


def freshness_key(paper):
    """Known public dates break score ties; newly ingested old papers get no bonus."""
    if paper.get('paper_date_basis') == 'ingested':
        return ''
    return paper.get('paper_date_sort') or paper.get('paper_date') or paper.get('published') or ''


def personal_score(similarity, paper, cfg):
    """Match the best interest; quality and real publication dates are aids."""
    published = freshness_key(paper)
    recent = 0.
    if published:
        try:
            age = max(0, (date.fromisoformat(today()) - date.fromisoformat(published[:10])).days)
            recent = 2 ** (-age / 30)
        except ValueError:
            pass
    quality = paper['quality_score'] if paper.get('scored') and paper.get('quality_score') is not None else NEUTRAL
    return 100 * cfg.interest_weight * max(0., similarity) + cfg.quality_weight * quality + 100 * cfg.novelty_weight * recent


def serialize_paper(paper, preferred=()):
    result = {k: v for k, v in paper.items() if k not in ('embedding', 'fulltext', 'skeleton')}
    result['authors'] = json.loads(paper.get('authors') or '[]')
    result['categories'] = json.loads(paper.get('categories') or '[]')
    result['brief'] = json.loads(paper['brief_json']) if paper.get('brief_json') else None
    result['source_label'] = source_label(paper, preferred)
    assessment = json.loads(paper['skeleton']) if paper.get('skeleton') else None
    result['quality_assessment'] = assessment
    unknown = assessment is not None and not has_content_score(assessment)
    result['quality_status'] = 'pending' if not paper.get('scored') else 'limited' if unknown or assessment and assessment.get('evidence_insufficient') else 'assessed'
    if result['quality_status']=='pending' or unknown:result['quality_score']=None
    return result


def balanced_papers(ranked):
    """Default discovery covers multiple sources before repeating a source."""
    groups = {}
    for paper in ranked:
        source = (paper.get('venue') or '').split('.')[0] or paper.get('primary_category') or 'other'
        groups.setdefault(source, deque()).append(paper)
    output = []
    while groups:
        for key in sorted(groups, key=lambda key: groups[key][0]['score'], reverse=True):
            output.append(groups[key].popleft())
            if not groups[key]:
                del groups[key]
    return output


def paper_category(structured, paper, topic_ids=None):
    """Assign a cross-listed paper once, preferring its selected primary source."""
    ids = topic_ids if topic_ids is not None else [t['id'] for t in paper.get('topics', [])]
    keys = matched_keys(structured, paper, ids) or []
    primary = 'venue:' + paper['venue'].split('.')[0] if paper.get('venue') else 'arxiv:' + (paper.get('primary_category') or '')
    return primary if primary in keys else next(iter(keys), None)


def weighted_order(ids, categories, weights, consumed=None):
    """Smooth weighted interleaving; queues keep their original score order."""
    groups = {}
    other = []
    for ident in ids:
        key = categories.get(ident)
        if key in weights:
            groups.setdefault(key, deque()).append(ident)
        else:
            other.append(ident)
    consumed = consumed or {}
    total_weight = sum(weights[key] for key in groups)
    displayed = sum(consumed.get(key, 0) for key in groups)
    credit = {key: displayed * weights[key] - consumed.get(key, 0) * total_weight for key in groups}
    output = []
    while groups:
        total_weight = sum(weights[key] for key in groups)
        for key in groups:
            credit[key] += weights[key]
        key = max(groups, key=lambda key: credit[key])
        credit[key] -= total_weight
        output.append(groups[key].popleft())
        if not groups[key]:
            del groups[key]
    return output + other


def consumed_categories(excluded, categories, structured):
    counts = Counter(categories[ident] for ident in excluded if ident in categories)
    missing = list(excluded - categories.keys())
    for start in range(0, len(missing), 500):
        batch = missing[start:start+500]
        # Seen papers can disappear from a rebuilt candidate cache. Keep their
        # display counts so later batches carry the fractional share forward.
        papers = rows('''SELECT p.id,p.primary_category,p.categories,p.venue,
            (SELECT json_group_array(pt.topic_id) FROM paper_topics pt JOIN topics t
             ON t.id=pt.topic_id WHERE pt.paper_id=p.id AND t.status='active') AS topic_ids
            FROM papers p WHERE p.id IN (''' + ','.join('?' for _ in batch) + ')', batch)
        counts.update(paper_category(structured, p, json.loads(p['topic_ids'])) for p in papers)
    return counts


def scoring_context(user_id, db=None, with_states=True):
    if db is not None:return _scoring_context(user_id,db,with_states)
    with connect() as conn:
        conn.execute('BEGIN')
        p=conn.execute('SELECT * FROM interest_profile WHERE user_id=? ORDER BY version DESC LIMIT 1',(user_id,)).fetchone()
        profile=dict(p) if p else None
        feedback=conn.execute("SELECT MAX(id) FROM interactions WHERE user_id=? AND action NOT IN ('expand','view')",(user_id,)).fetchone()[0]
        revision=conn.execute("SELECT value FROM app_settings WHERE name='paper_revision'").fetchone()[0]
        space=vector_store.active(conn)
        weights=recommendation_settings.configuration(conn)
        key=(str(settings().data_dir.absolute()),user_id,with_states,tuple(profile[k] for k in ('id','content','structured','embedding','embedding_parts')) if profile else None,feedback,revision,today(),space['name'] if space else None,recommendation_settings.cache_key(weights))
        with _context_lock:
            cached=_context_cache.get(key)
            if cached and time.monotonic()-cached[0]<1:return cached[1]
            value=_scoring_context(user_id,conn,with_states,profile,weights)
            _context_cache[key]=(time.monotonic(),value)
            while len(_context_cache)>64:_context_cache.popitem(last=False)
            return value


_context_cache=OrderedDict()
_context_lock=threading.Lock()


def _scoring_context(user_id, db=None, with_states=True, profile=None, weights=None):
    query=lambda sql,args: [dict(r) for r in db.execute(sql,args)] if db is not None else rows(sql,args)
    if profile is None:
        p=db.execute('SELECT * FROM interest_profile WHERE user_id=? ORDER BY version DESC LIMIT 1',(user_id,)).fetchone() if db else current(user_id)
        profile=dict(p) if p else None
    vector = active_vectors(profile)
    entries = active_entries(profile['content']) if profile else []
    structured = json.loads(profile['structured']) if profile else {}
    selection = structured.get('category_selection')
    selected = set(structured.get('topic_ids', [])) if selection is None else set()
    cutoff = (date.fromisoformat(today()) - timedelta(days=7)).isoformat()
    seen = query("SELECT pt.topic_id,COUNT(DISTINCT i.paper_id) AS n FROM interactions i JOIN paper_topics pt ON pt.paper_id=i.paper_id WHERE i.user_id=? AND i.created_at>=? AND i.action IN ('skip','like') AND NOT EXISTS(SELECT 1 FROM interactions u WHERE u.target_id=i.id AND u.action='undo') GROUP BY pt.topic_id", (user_id, cutoff))
    counts = Counter({r['topic_id']: r['n'] for r in seen})
    total = sum(counts.values()) or 1
    cfg = recommendation_settings.scoring_settings(weights if weights is not None else recommendation_settings.configuration(db))
    states = {s['paper_id']: s for s in query('SELECT paper_id,liked,saved FROM user_paper_state WHERE user_id=? AND (liked=1 OR saved=1)', (user_id,))} if with_states else {}
    return profile,vector,entries,structured,selection,selected,counts,total,cfg,states,vector_store.active(db) or False


def scored_papers(user_id, candidates, restrict=False, *, context=None, db=None, ranking_only=False, with_directions=False):
    profile,vector,entries,structured,selection,selected,counts,total,cfg,states,space = context or scoring_context(user_id,db,not ranking_only)
    # Guests do not use semantic scores: avoid reading vectors for their pages.
    if vector:
        vector_store.hydrate(candidates,space=space,db=db)
    query=lambda sql,args: [dict(r) for r in db.execute(sql,args)] if db is not None else rows(sql,args)
    ids = [p['id'] for p in candidates]
    mapping = {}
    for start in range(0,len(ids),500):
        batch = ids[start:start+500]
        for t in query("SELECT pt.paper_id,pt.topic_id,t.name_zh,t.name_en FROM paper_topics pt JOIN topics t ON t.id=pt.topic_id WHERE t.status='active' AND pt.paper_id IN ("+','.join('?' for _ in batch)+')',batch):
            mapping.setdefault(t['paper_id'], []).append(t)
    excluded = [e for e in entries if e['excluded']]
    terms = {}
    for entry in entries:
        if entry['excluded'] or entry['weight'] <= 0:
            continue
        for term in re.split(r'[\s（()）、,]+', entry['text'].casefold()):
            if len(term) > 1:
                terms[term] = max(terms.get(term, 0), entry['weight'])
    output = []
    for p in candidates:
        tags = mapping.get(p['id'], [])
        if restrict and (not paper_keys(p) or not in_scope(structured, p, [t['topic_id'] for t in tags])):
            continue
        text = (p['title'] + ' ' + p['abstract'] + ' ' + ' '.join(t['name_zh']+' '+t['name_en'] for t in tags)).casefold()
        if any(e['text'].casefold() in text or any(len(term)>2 and term.casefold() in text for term in re.split(r'[（()）、,]', e['text'])) for e in excluded):
            continue
        scoped = {tid for key, ids in selection.get('topics', {}).items() if matches(p, key) for tid in ids} if selection else set()
        category_hit = any(matches(p, key) for key in selection.get('categories', [])) if selection else False
        hit = [t for t in tags if t['topic_id'] in selected or t['topic_id'] in scoped]
        direction = ''
        similarity = 0.
        if vector and p['embedding']:
            matched,similarity = best_match(vector, unpack(p['embedding']))
            interest = (similarity + 1)*50
            direction = matched['key']
        else:
            interest = min(85, 40 + sum(10 * weight for term, weight in terms.items() if term in text))
            if vector:
                matches_by_entry = [(sum(term in text for term in re.split(r'[\s（()）、,]+',part['key'].casefold()) if len(term)>1),part) for part in vector if part['key']]
                if matches_by_entry:
                    count,part = max(matches_by_entry,key=lambda item:(item[0],item[1]['weight']))
                    if count:direction=part['key']
        interest = min(100, interest + (15 if hit or category_hit else 0))
        novelty = max(0, 100 * (1 - max((counts[t['topic_id']]/total for t in tags), default=0)))
        if profile and vector:
            score = personal_score(similarity, p, cfg)
        elif profile:
            score = cfg.interest_weight*interest + cfg.quality_weight*(p['quality_score'] if p.get('scored') and p['quality_score'] is not None else NEUTRAL) + cfg.novelty_weight*novelty
        else:
            age = max(0, (date.fromisoformat(today()) - date.fromisoformat(p['published'] or p['ingested_date'])).days)
            score = cfg.guest_quality_weight*(p['quality_score'] if p.get('scored') and p['quality_score'] is not None else NEUTRAL) + cfg.guest_recency_weight*max(0, 100-age*5)
        # One saved flag distinguishes a measured zero from unavailable author data.
        author_weight = cfg.author_weight if profile else cfg.guest_author_weight
        if p.get('author_impact_known') or bounded(p.get('author_impact')) > 0:
            score += author_weight * bounded(p.get('author_impact'))
        else:
            score = score / (1-author_weight) if author_weight < 1 else NEUTRAL
        score = min(100., score)
        if ranking_only:
            output.append({'id':p['id'],'score':round(score,1),'published':p['published'],'ingested_date':p['ingested_date'],'freshness':freshness_key(p)})
            continue
        item = serialize_paper(p, matched_keys(structured, p, [t['topic_id'] for t in tags]))
        state = states.get(p['id'])
        item.update(liked=bool(state and state['liked']), saved=bool(state and state['saved']))
        item.update(score=round(score,1), topics=[{'id':t['topic_id'], 'name_zh':t['name_zh'], 'name_en':t['name_en']} for t in tags],
                    reason=('与您关注的「'+hit[0]['name_zh']+'」相关') if hit else ('与您的研究画像语义相近' if vector and p['embedding'] else '结合论文质量与主题多样性推荐'),
                    exploration=bool(tags and all(t['topic_id'] not in counts and t['topic_id'] not in selected for t in tags)))
        if with_directions:item['_direction']=direction
        output.append(item)
    return sorted(output, key=lambda p: (p['score'],p.get('freshness',freshness_key(p))), reverse=True)


_ranking_cache = OrderedDict()
_cache_lock = threading.Lock()
_ranking_locks = [threading.RLock() for _ in range(64)]
_query_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='recommendation-read')
_query_slots = threading.BoundedSemaphore(2)


def parallel_reads(queries):
    # Shared by all requests: two running reads, at most two queued batches.
    with _query_slots:
        return list(_query_pool.map(lambda query: rows(*query), queries))


def clear_user_cache(user_id):
    with _cache_lock:
        for key in list(_ranking_cache):
            if key[1]==user_id:_ranking_cache.pop(key,None)


def source_clause(key):
    return 'p.id IN (SELECT paper_id FROM paper_categories WHERE category_key=?)',[key.casefold()]


def scope_clause(structured, guest=False):
    from ..source_catalog import supported_keys
    available=supported_keys(guest=guest)
    if not available:return '0',[]
    if structured.get('source_selection_removed') and not has_selection(structured):return '0',[]
    selection=structured.get('category_selection')
    clauses,args=[],[]
    if has_selection(structured) and selection is not None:
        for key in selection.get('categories',[]):
            if key not in available:continue
            clause,values=source_clause(key);clauses.append('('+clause+')');args.extend(values)
        for key,ids in selection.get('topics',{}).items():
            if not ids or key not in available:continue
            clause,values=source_clause(key)
            clauses.append('('+clause+' AND EXISTS(SELECT 1 FROM paper_topics pt WHERE pt.paper_id=p.id AND pt.topic_id IN ('+','.join('?' for _ in ids)+')))')
            args.extend(values+ids)
    else:
        for key in sorted(available):
            clause,values=source_clause(key);clauses.append('('+clause+')');args.extend(values)
        if structured.get('topic_ids'):
            ids=structured['topic_ids']
            return '('+' OR '.join(clauses)+') AND EXISTS(SELECT 1 FROM paper_topics pt WHERE pt.paper_id=p.id AND pt.topic_id IN ('+','.join('?' for _ in ids)+'))',args+ids
    return '('+' OR '.join(clauses)+')' if clauses else '0',args


def candidate_pool(user_id, context, excluded=(), scoring=None, *, budget=None):
    scoring=scoring or scoring_context(user_id)
    profile,vectors=scoring[:2]
    structured=json.loads(profile['structured']) if profile else {}
    scope,args=scope_clause(structured,guest=user_id is None)
    cutoff=(date.fromisoformat(today())-timedelta(days=14)).isoformat()
    period = {'recommendations': '1',
              'today': '(p.published>=? OR p.ingested_date>=?)',
              'history': "(COALESCE(p.published,'')<? AND COALESCE(p.ingested_date,'')<?)",
              'backlog': '(p.published>=? AND p.ingested_date<?)'}[context]
    period_args = [cutoff,cutoff] if context in ('today','history') else [cutoff,today()] if context=='backlog' else []
    tail=period+' AND NOT EXISTS(SELECT 1 FROM user_paper_state st WHERE st.user_id=? AND st.paper_id=p.id AND (st.seen=1 OR st.liked=1 OR st.saved=1 OR st.dismissed=1))'
    tail_args=[*period_args,user_id]
    if excluded:
        tail+=' AND p.id NOT IN (SELECT value FROM json_each(?))';tail_args.append(dumps(sorted(excluded)))
    clause=scope+' AND '+tail
    args.extend(tail_args)
    budget=settings().recommendation_candidates if budget is None else budget
    semantic_budget,quality_budget,fresh_budget=quotas([3 if vectors else 0,1 if vectors else 4,1],budget)
    queries = [('SELECT COUNT(*) n FROM papers p INDEXED BY idx_papers_dates WHERE '+clause,args),
        ('SELECT p.id FROM papers p INDEXED BY idx_papers_paper_date WHERE '+clause+" AND p.paper_date_basis!='ingested' ORDER BY p.paper_date_sort DESC,p.id DESC LIMIT ?",[*args,fresh_budget])]
    if vectors:
        revision=one("SELECT value FROM app_settings WHERE name='paper_revision'")['value']
        nearest=set()
        for part,quota in zip(vectors,quotas([p['weight'] for p in vectors],semantic_budget)):
            if quota:nearest.update(nearest_ids(part['blob'],quota,revision,scoring[-1]))
        if nearest:
            # Start from the bounded KNN IDs, then PK lookups. An IN predicate
            # after the source OR can make SQLite enumerate entire categories.
            queries.append(('SELECT p.id FROM json_each(?) v CROSS JOIN papers p ON p.id=v.value WHERE '+clause,
                            [dumps(sorted(nearest)),*args]))
    # Source coverage and semantic directions share a fixed total candidate budget.
    weights = category_weights(structured)
    if weights:
        # A global quality/KNN cutoff must not discard a smaller selected
        # category before weighted presentation has a chance to include it.
        selection = structured['category_selection']
        for key,quota in zip(weights,quotas(list(weights.values()),quality_budget)):
            if not quota:continue
            extra, values = scope_clause({'category_selection': {
                'categories': [key] if key in selection.get('categories', []) else [],
                'topics': {key: selection.get('topics', {}).get(key, [])}}})
            queries.append(('SELECT p.id FROM papers p INDEXED BY idx_papers_rank WHERE '+extra+' AND '+tail+' ORDER BY p.quality_score DESC,p.published DESC,p.id DESC LIMIT ?', [*values, *tail_args, quota]))
    elif not profile:
        from ..source_catalog import supported_keys
        keys=sorted(supported_keys(guest=user_id is None))
        for key,quota in zip(keys,quotas([1]*len(keys),quality_budget)):
            if not quota:continue
            extra,values=source_clause(key)
            queries.append(('SELECT p.id FROM papers p INDEXED BY idx_papers_rank WHERE '+extra+' AND '+tail+' ORDER BY p.quality_score DESC,p.published DESC,p.id DESC LIMIT ?',[*values,*tail_args,quota]))
    else:
        queries.append(('SELECT p.id FROM papers p INDEXED BY idx_papers_rank WHERE '+clause+' ORDER BY p.quality_score DESC,p.published DESC,p.id DESC LIMIT ?',[*args,quality_budget]))
    results=parallel_reads(queries)
    total=results[0][0]['n']
    ids=sorted({p['id'] for result in results[1:] for p in result})
    queries=[]
    for start in range(0,len(ids),500):
        batch=ids[start:start+500]
        queries.append(('SELECT '+PAPER_COLUMNS+' FROM papers p WHERE p.id IN ('+','.join('?' for _ in batch)+')',batch))
    papers=[p for batch in parallel_reads(queries) for p in batch]
    return papers,total


def returning_pool(user_id, context, scoring, before, budget):
    """An indexed, bounded scan of this user's cooled history, not all papers."""
    profile=scoring[0]
    scope,args=scope_clause(json.loads(profile['structured']) if profile else {})
    clause='s.user_id=? AND s.last_browsed_at<=? AND s.seen=1 AND s.dismissed=0 AND s.liked=0 AND s.saved=0'
    values=[user_id,before]
    if context=='backlog':
        clause+=' AND p.published>=? AND p.ingested_date<?'
        values.extend([(date.fromisoformat(today())-timedelta(days=14)).isoformat(),today()])
    return rows('SELECT '+PAPER_COLUMNS+' FROM user_paper_state s INDEXED BY idx_state_browsed JOIN papers p ON p.id=s.paper_id WHERE '+clause+' AND '+scope+' ORDER BY s.last_browsed_at DESC,s.paper_id DESC LIMIT ?',[*values,*args,budget])


def ranking_key(user_id, context):
    profile=current(user_id)
    feedback=one("SELECT MAX(id) n FROM interactions WHERE user_id=? AND action NOT IN ('expand','view')",(user_id,))['n']
    changes=one("SELECT value FROM app_settings WHERE name='paper_revision'")['value']
    cfg=settings()
    weights=recommendation_settings.cache_key(recommendation_settings.configuration())
    return (str(cfg.data_dir.absolute()),user_id,context,today(),profile['id'] if profile else None,profile['version'] if profile else None,profile['embedding'] if profile else None,profile['embedding_parts'] if profile else None,feedback,changes,*weights,vector_store.revision())


def ranked_page(user_id, context, offset, limit, exclude_ids=()):
    key=ranking_key(user_id,context)
    # Coalesce concurrent cold requests for the same ranking without holding
    # the global cache lock over database reads or scoring.
    with _ranking_locks[hash(key) % len(_ranking_locks)]:
        return _ranked_page(key,user_id,context,offset,limit,exclude_ids)


def ordered_remaining(saved, hidden, excluded, structured, weights, directions=None):
    remaining=[i for i in saved['ids'] if i not in hidden]
    # Preserve explicit source proportions without forcing interest interleaving.
    directions={}
    if not weights and not directions:
        return remaining
    categories=saved['categories']
    consumed=consumed_categories(excluded,categories,structured) if weights else Counter()
    assignments=saved.get('directions',{})
    direction_counts=Counter(assignments[i] for i in excluded if i in assignments)
    def mix(ids):
        if directions:
            if weights:
                # Keep source proportions authoritative; mix interests within each source.
                by_source={}
                for ident in ids:by_source.setdefault(categories.get(ident),[]).append(ident)
                ids=[ident for group in by_source.values() for ident in weighted_order(group,assignments,directions,direction_counts)]
            else:ids=weighted_order(ids,assignments,directions,direction_counts)
        return weighted_order(ids,categories,weights,consumed) if weights else ids
    recent=[i for i in remaining if saved['periods'][i]=='today']
    older=[i for i in remaining if saved['periods'][i]!='today']
    recent=mix(recent)
    consumed.update(categories[i] for i in recent)
    direction_counts.update(assignments[i] for i in recent if i in assignments)
    return recent+mix(older)


def _ranked_page(key,user_id,context,offset,limit,exclude_ids):
    scoring=scoring_context(user_id)
    profile = scoring[0]
    structured = json.loads(profile['structured']) if profile else {}
    weights = category_weights(structured)
    directions={part['key']:part['weight'] for part in scoring[1] if part['key']}
    excluded=set(exclude_ids)
    before=browsing.cutoff()
    with _cache_lock:
        saved=_ranking_cache.get(key)
        if saved and time.monotonic()-saved['time']<settings().recommendation_cache_seconds:
            _ranking_cache.move_to_end(key)
        else:saved=None
    if not saved:
        saved={'time':time.monotonic(),'ids':[],'scores':{},'categories':{},'directions':{},'returns':[],
               'periods':{},'processed':set(),'stage':'recommendations' if context=='today' and scoring[1] else context,'more':True}
        cooled=one('''SELECT 1 FROM user_paper_state INDEXED BY idx_state_browsed WHERE user_id=?
            AND last_browsed_at<=? AND seen=1 AND liked=0 AND saved=0 AND dismissed=0 LIMIT 1''',(user_id,before)) if user_id is not None else None
        reserve=min(100,settings().recommendation_candidates//10) if cooled else 0
        if reserve:
            returned=scored_papers(user_id,returning_pool(user_id,context,scoring,before,reserve),restrict=True,context=scoring)
            saved['returns']=[p['id'] for p in returned]
            saved['scores'].update({p['id']:p['score'] for p in returned})
            saved['categories'].update({p['id']:paper_category(structured,p) for p in returned})
        saved['reserve']=reserve if saved['returns'] else 0
    # Inspect only this bounded ranking's IDs, rather than loading a user's
    # entire browsing history on every request.
    with connect() as db:seen=browsing.states(db,user_id,saved['ids']+saved['returns'])
    eligible={ident for ident,state in seen.items() if browsing.returning(state,before)}
    hidden=excluded|(seen.keys()-eligible)
    remaining=[i for i in saved['ids'] if i not in hidden and i not in seen]
    attempts=0
    while len(remaining)<offset+limit and saved['more'] and attempts<3:
        stage=saved['stage']
        papers,total=candidate_pool(user_id,stage,tuple(excluded|saved['processed']),scoring,
                                   budget=settings().recommendation_candidates-saved['reserve'])
        ranked=scored_papers(user_id,papers,restrict=True,context=scoring,with_directions=True)
        if not profile:ranked=balanced_papers(ranked)
        if not profile and context!='recommendations' and not weights:
            rng=random.Random(f'{user_id}:{today()}:{context}')
            for start in range(0,len(ranked),5):
                window=ranked[start:start+5];rng.shuffle(window);ranked[start:start+5]=window
            for position in range(6,len(ranked),7):
                index=next((i for i in range(position,len(ranked)) if ranked[i]['exploration']),None)
                if index is not None:ranked.insert(position,ranked.pop(index))
        saved['processed'].update(p['id'] for p in papers)
        saved['ids'].extend(p['id'] for p in ranked)
        saved['scores'].update({p['id']:p['score'] for p in ranked})
        saved['categories'].update({p['id']:paper_category(structured,p) for p in ranked})
        saved['directions'].update({p['id']:p['_direction'] for p in ranked})
        saved['periods'].update({p['id']:stage for p in ranked})
        saved['more']=total>len(papers)
        if stage=='today' and not saved['more']:
            # History is only queried when the recent queue cannot fill a page.
            saved['stage']='history'
            saved['more']=True
        saved['time']=time.monotonic()
        attempts+=1
        remaining=[i for i in saved['ids'] if i not in hidden and i not in seen]
    with _cache_lock:
        _ranking_cache[key]=saved
        while len(_ranking_cache)>256 or sum(len(item.get('processed',item['ids'])) for item in _ranking_cache.values())>100000:
            _ranking_cache.popitem(last=False)
    remaining=ordered_remaining(saved,hidden|seen.keys(),excluded,structured,weights,directions)
    returned=[i for i in saved['returns'] if i in eligible and i not in excluded]
    if returned:
        # Select a source using the existing proportions, then consider a
        # cooled paper within that source under the global repeat limit.
        return_ids=set(saved['returns'])
        previous_returns=[index-len(exclude_ids) for index,ident in enumerate(exclude_ids)
                          if index>=len(exclude_ids)-19 and ident in return_ids]
        remaining=browsing.mix(remaining,returned,saved['scores'],
                              saved['categories'] if weights else None,weights,
                              consumed_categories(excluded,saved['categories'],structured) if weights else None,
                              previous_returns=previous_returns,presented=len(exclude_ids),
                              priorities={i:0 if saved['periods'][i]=='today' else 1 for i in remaining} if weights else None)
    chosen=remaining[offset:offset+limit]
    papers=rows('SELECT '+PAPER_COLUMNS+' FROM papers p WHERE p.id IN ('+','.join('?' for _ in chosen)+')',chosen) if chosen else []
    items={p['id']:p for p in scored_papers(user_id,papers,restrict=True,context=scoring)}
    for ident,item in items.items():
        item['score']=saved['scores'][ident]
        if ident in eligible:item['last_browsed_at']=seen[ident]['last_browsed_at']
        if weights and saved['categories'].get(ident):
            item['source_label'] = source_label(item, [saved['categories'][ident]])
    return {'items':[items[i] for i in chosen if i in items],
            'total':len(remaining)+(1 if saved['more'] and len(remaining)<=offset+limit else 0)}


def browse_page(user_id,clause,args,sort,offset,limit,date_basis='paper'):
    if sort=='date':
        index=' INDEXED BY '+('idx_papers_ingested_sort' if date_basis=='ingested' else 'idx_papers_paper_date')
        date_column='p.ingested_date' if date_basis=='ingested' else 'p.paper_date_sort'
        if 'paper_search' in clause:
            page=one('WITH matched AS MATERIALIZED (SELECT p.id FROM papers p'+index+' WHERE '+clause+'), page AS (SELECT p.id FROM papers p'+index+' WHERE p.id IN (SELECT id FROM matched) ORDER BY '+date_column+' DESC,p.id DESC LIMIT ? OFFSET ?) SELECT (SELECT COUNT(*) FROM matched) n,(SELECT json_group_array(id) FROM page) ids',[*args,limit,offset])
            total=page['n'];chosen=json.loads(page['ids'])
            found=rows('SELECT '+PAPER_COLUMNS+' FROM papers p WHERE p.id IN ('+','.join('?' for _ in chosen)+')',chosen) if chosen else []
            by_id={p['id']:p for p in found}
            papers=[by_id[i] for i in chosen]
        else:
            total=one('SELECT COUNT(*) n FROM papers p'+index+' WHERE '+clause,args)['n']
            papers=rows('SELECT '+PAPER_COLUMNS+' FROM papers p'+index+' WHERE '+clause+' ORDER BY '+date_column+' DESC,p.id DESC LIMIT ? OFFSET ?',[*args,limit,offset])
        items={p['id']:p for p in scored_papers(user_id,papers)}
        return {'items':[items[p['id']] for p in papers if p['id'] in items],'total':total}
    date_revision=one("SELECT value FROM app_settings WHERE name='paper_date_revision'")['value'] if 'paper_date' in clause else None
    key=ranking_key(user_id,('browse',clause,tuple(args),date_revision))
    with _cache_lock:
        saved=_ranking_cache.get(key)
        if saved and time.monotonic()-saved['time']>=settings().recommendation_cache_seconds:saved=None
    if not saved:
        ranked=[]
        # One streaming query and shared scoring inputs: no repeated full category scan,
        # profile/feedback reads, or serialization of thousands of off-screen cards.
        with connect() as db:
            db.execute('BEGIN')
            context=scoring_context(user_id,db,False)
            fields='p.id,p.title,p.abstract,p.embedding,p.quality_score,p.scored,p.author_impact,p.author_impact_known,p.published,p.paper_date,p.paper_date_sort,p.paper_date_basis,p.ingested_date,p.categories,p.primary_category,p.venue'
            cursor=db.execute('SELECT '+fields+' FROM papers p WHERE '+clause,args)
            while batch:=cursor.fetchmany(400):
                ranked.extend((p['id'],p['score'],p['freshness']) for p in scored_papers(user_id,[dict(r) for r in batch],context=context,db=db,ranking_only=True))
        ranked.sort(key=lambda p:(p[1],p[2],p[0]),reverse=True)
        saved={'time':time.monotonic(),'ids':[p[0] for p in ranked],'scores':{p[0]:p[1] for p in ranked},'more':False}
        with _cache_lock:
            _ranking_cache[key]=saved
            while len(_ranking_cache)>256 or sum(len(item['ids']) for item in _ranking_cache.values())>100000:_ranking_cache.popitem(last=False)
    ids=saved['ids'][offset:offset+limit]
    papers=rows('SELECT '+PAPER_COLUMNS+' FROM papers p WHERE p.id IN ('+','.join('?' for _ in ids)+')',ids) if ids else []
    items={p['id']:p for p in scored_papers(user_id,papers)}
    for ident,item in items.items():item['score']=saved['scores'][ident]
    return {'items':[items[i] for i in ids if i in items],'total':len(saved['ids'])}


def feed(user_id, context, offset, limit, exclude_ids=()):
    result=ranked_page(user_id,context,offset,limit,exclude_ids)
    profile=current(user_id)
    pending=one('SELECT COUNT(*) n FROM papers WHERE embedding IS NULL')['n']
    return {**result,'context':context,'personalization':{'has_profile':bool(profile),'profile_ready':bool(active_vectors(profile)),'pending_paper_vectors':pending}}
