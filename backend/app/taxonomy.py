"""Source membership and flat topics from the administrator's registry."""
import json
from .source_catalog import registry, supported_keys


def topic_keys(topic):
    return set(json.loads(topic.get('category_keys') or '[]')) & supported_keys(False)


def paper_keys(paper):
    sources=registry(True)
    if paper.get('venue'):
        return {s['key'] for s in sources if s['kind']=='venue' and paper['venue'].split('.')[0].casefold()==s['code'].casefold()}
    categories=paper.get('categories') or []
    if isinstance(categories,str):categories=json.loads(categories)
    codes=set(categories)
    if paper.get('primary_category'):codes.add(paper['primary_category'])
    return {'arxiv:'+code for code in codes} & {s['key'] for s in sources}


def scoped_topics(paper,topics):
    keys=paper_keys(paper)
    return [topic for topic in topics if topic_keys(topic)&keys]
