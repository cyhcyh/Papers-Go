"""Hybrid retrieval over standard labels and their full taxonomy paths."""
import math
import re
from collections import Counter
from functools import lru_cache


def normalize(text):
    return re.sub(r'[^a-z0-9]+', ' ', text.casefold()).strip()


def tokens(text):
    stop = set('the and for with from using based into that this these our their which than are can has have its such also study propose proposed introduce show result results approach'.split())
    return [w[:-1] if w.endswith('s') and len(w)>4 else w
            for w in re.findall(r'[a-z]{3,}', text.casefold()) if w not in stop]


def concept(entry):
    return entry['system'] + ':' + entry['code'].split('.')[-1]


@lru_cache(maxsize=2)
def make_index(entries):
    docs = {key: Counter(tokens(label + ' ' + path)) for key,label,path in entries}
    df = Counter(w for doc in docs.values() for w in doc)
    idf = {w: math.log(1 + (len(docs)-n+.5)/(n+.5)) for w,n in df.items()}
    average = sum(sum(doc.values()) for doc in docs.values()) / max(1,len(docs))
    labels = {key:(set(tokens(label)),normalize(label)) for key,label,path in entries}
    return docs,idf,average,labels


def retrieve(paper, entries, limit=40, semantic_scores=None, source_disciplines=(), source_labels=()):
    query = Counter(tokens(paper['title'] + ' ' + (paper.get('abstract') or '')))
    source_words=set(tokens(' '.join(source_labels)))
    title_words = set(tokens(paper['title']))
    padded = ' '+normalize(paper['title']+' '+(paper.get('abstract') or ''))+' '
    docs,idf,average,labels = make_index(tuple((e['key'],e['label'],e['path']) for e in entries))
    lexical = []
    for e in entries:
        doc=docs[e['key']];length=sum(doc.values());label_words,phrase=labels[e['key']]
        score=0.
        for word in (query.keys() | source_words) & doc.keys():
            frequency=doc[word]
            bm=frequency*2.2/(frequency+1.2*(.25+.75*length/average))
            weight=min(2,1+math.log(query[word])) if query[word] else .35
            score+=idf[word]*bm*(1.5 if word in title_words else 1)*(1 if word in label_words else .3)*weight
        if ' ' in phrase and ' '+phrase+' ' in padded:score+=10
        lexical.append((score,e))
    lexical.sort(key=lambda item:(-item[0],item[1]['key']))
    ranks={e['key']:i+1 for i,(_,e) in enumerate(lexical)}
    if semantic_scores:
        semantic_ranks={key:i+1 for i,(key,_) in enumerate(sorted(semantic_scores.items(),key=lambda item:-item[1]))}
        def fused(entry):
            key=entry['key']
            return .4/(20+ranks[key]) + (.6/(20+semantic_ranks[key]) if key in semantic_ranks else 0)
        ranked=sorted(entries,key=lambda e:(-fused(e),e['key']))
    else:
        ranked=[e for _,e in lexical];semantic_ranks={}
    by_key={e['key']:e for e in entries}
    ancestors=[]
    for entry in ranked[:8]:
        parent=entry.get('parent')
        for _ in range(2):
            if parent not in by_key:break
            ancestors.append(by_key[parent]);parent=by_key[parent].get('parent')
    result=[];seen=set()
    # Reserve a quarter of the shortlist for source disciplines without excluding
    # cross-disciplinary matches. The rest still follows the hybrid ranking.
    source_options={d:[e for e in ranked if e.get('discipline')==d] for d in sorted(source_disciplines)}
    quota=min(10,max(1,limit//4))
    reserved=[]
    # Include each source discipline when the paper is cross-listed.
    for i in range(quota):
        for matching in source_options.values():
            if i<len(matching):reserved.append(matching[i])
            if len(reserved)>=quota:break
        if len(reserved)>=quota:break
    for entry in reserved+ranked[:max(1,limit-8)]+ancestors+ranked:
        ident=concept(entry)
        if ident in seen:continue
        seen.add(ident)
        result.append({**entry,**({'semantic_score':semantic_scores.get(entry['key']),
                                 'semantic_rank':semantic_ranks.get(entry['key'])} if semantic_scores else {})})
        if len(result)>=limit:break
    order={entry['key']:i for i,entry in enumerate(ranked)}
    return sorted(result,key=lambda entry:order[entry['key']])
