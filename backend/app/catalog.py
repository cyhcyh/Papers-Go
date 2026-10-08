"""Browse sources and their research topics, independent of the global topic tree."""
import json
from typing import Annotated
from fastapi import HTTPException
from pydantic import BaseModel, Field
from .db import rows
from .taxonomy import topic_keys
from .source_catalog import registry, supported_keys

DEFAULT_CATEGORY_WEIGHT = .7


class CategorySelection(BaseModel):
    categories: list[str] = Field(default_factory=list)
    topics: dict[str, list[int]] = Field(default_factory=dict)
    weights: dict[str, Annotated[float, Field(gt=0, le=1, allow_inf_nan=False)]] = Field(default_factory=dict)


def matches(paper, key):
    kind, code = key.split(':', 1)
    if kind == 'venue':
        return (paper.get('venue') or '').split('.')[0].casefold() == code.casefold()
    if paper.get('venue'):
        return False
    categories = paper.get('categories') or []
    if isinstance(categories, str):
        categories = json.loads(categories)
    return code == paper.get('primary_category') or code in categories


def source_label(paper, preferred=()):
    sources=registry()
    if paper.get('venue'):
        return next((s['code'] for s in sources if s['kind']=='venue' and s['code'].casefold()==paper['venue'].split('.')[0].casefold()), paper['venue'].split('.')[0])
    keys = list(preferred or [])
    definitions = [(s['key'],s['code'] if s['kind']=='venue' else s['label']+' · '+s['code']) for s in sources]
    if not keys:
        primary = 'arxiv:' + (paper.get('primary_category') or '')
        keys = [key for key, _ in definitions if matches(paper, key)]
        if primary in keys and not paper.get('venue'):
            keys.remove(primary)
            keys.insert(0, primary)
    labels = dict(definitions)
    return next((labels[key] for key in keys if key in labels and matches(paper, key)), paper.get('venue') or paper.get('primary_category') or '研究论文')


def directory(include_proposed=False, *, with_counts=True):
    topics = rows("SELECT id,name_zh,name_en,parent_id,status,category_keys FROM topics WHERE status IN ('active','proposed')" if include_proposed else "SELECT id,name_zh,name_en,parent_id,status,category_keys FROM topics WHERE status='active'")
    counts = {r['category_key']:r['n'] for r in rows('SELECT category_key,COUNT(*) n FROM paper_categories GROUP BY category_key')} if with_counts else {}
    linked = {(r['category_key'],r['topic_id']):r['n'] for r in rows('SELECT c.category_key,pt.topic_id,COUNT(*) n FROM paper_categories c JOIN paper_topics pt ON pt.paper_id=c.paper_id GROUP BY c.category_key,pt.topic_id')} if with_counts else {}
    sources=registry(enabled_only=not include_proposed)
    output = []
    for source in sources:
        kind,code,label,key=source['kind'],source['code'],source['label'],source['key']
        scoped = []
        for t in topics:
            if key not in topic_keys(t):
                continue
            scoped.append({k: t[k] for k in ('id', 'name_zh', 'name_en', 'status')} | {
                'parent_id': None, 'paper_count': linked.get((key.casefold(),t['id']),0)})
        output.append({'key': key, 'kind': kind, 'code': code, 'label': label,
                       'discipline':source['discipline'],'discipline_label':source['discipline_label'],
                       'label_en':source['label_en'],'label_zh':source['label_zh'],
                       'enabled':bool(source['enabled']),'standard_system':source['standard_system'],
                       'group': '顶会' if kind == 'venue' else 'arXiv',
                       'paper_count': counts.get(key.casefold(),0), 'topics': scoped})
    return output


def validate_category_keys(keys, include_inactive=False):
    if not set(keys) <= supported_keys(not include_inactive):
        raise HTTPException(400, '分类不存在或尚未开放')
    return list(dict.fromkeys(keys))


def validate_selection(selection, catalog=None):
    if isinstance(selection, CategorySelection):
        selection = selection.model_dump()
    catalog = {c['key']: c for c in (directory(with_counts=False) if catalog is None else catalog)}
    categories = list(dict.fromkeys(selection.get('categories', [])))
    partial = selection.get('topics', {})
    if not set(categories) <= catalog.keys() or not set(partial) <= catalog.keys():
        raise HTTPException(400, '分类不存在或尚未开放')
    for key, ids in partial.items():
        if not set(ids) <= {t['id'] for t in catalog[key]['topics']}:
            raise HTTPException(400, '主题不属于所选分类')
    topics = {k: list(dict.fromkeys(v)) for k, v in partial.items() if v and k not in categories}
    selected = list(dict.fromkeys([*categories, *topics]))
    weights = selection.get('weights', {})
    if not set(weights) <= set(selected):
        raise HTTPException(400, '只能为已关注的分类设置关注程度')
    return {'categories': categories, 'topics': topics,
            'weights': {key: weights.get(key, DEFAULT_CATEGORY_WEIGHT) for key in selected}}


def selected_topic_ids(selection, catalog=None):
    selected = {tid for ids in selection.get('topics', {}).values() for tid in ids}
    for category in (directory(with_counts=False) if catalog is None else catalog):
        if category['key'] in selection.get('categories', []):
            selected.update(t['id'] for t in category['topics'])
    return sorted(selected)
