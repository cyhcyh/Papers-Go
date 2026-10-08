"""Readable references derived from existing paper titles, without model calls."""
import json
import re


def reference(paper):
    brief=json.loads(paper.get('brief_json') or '{}')
    display=brief.get('title_zh') or paper['title']
    prefix=re.split(r'[:：]',display,maxsplit=1)[0].strip()
    short=prefix if 3<=len(prefix)<=45 and prefix!=display else display
    if len(short)>64:short=short[:63]+'…'
    return {'title':paper['title'],'display_title':display,'short_title':short,'url':paper.get('abs_url')}
