"""Explicit legacy fixtures for migration regression tests; never used by the app."""
"""Explicit source membership for a flat list of research topics."""
import json
from app.config import now


ARXIV = [
    ('cs.AI', '人工智能', []), ('cs.LG', '机器学习', []),
    ('cs.CL', '自然语言处理', []), ('cs.CV', '计算机视觉', []),
    ('cs.MA', '多智能体', []), ('cs.NE', '神经与进化计算', []),
    ('cs.RO', '机器人', []), ('stat.ML', '统计机器学习', []),
    ('math.CO', '组合数学', []),
]
VENUES = ['ICML', 'AAAI', 'NeurIPS', 'ICLR']
SUPPORTED_KEYS = {'arxiv:' + code for code, _, _ in ARXIV} | {'venue:' + code for code in VENUES}
AI_VENUES = ['venue:' + code for code in VENUES]


def scopes(*codes, conferences=True):
    return ['arxiv:' + code for code in codes] + (AI_VENUES if conferences else [])


# Each entry is a peer topic, directly below its explicitly assigned sources.
TOPIC_SEEDS = [
    ('智能体', 'Agents', scopes('cs.AI', 'cs.MA')),
    ('记忆与长上下文', 'Memory and long context', scopes('cs.AI', 'cs.CL')),
    ('智能体评测', 'Agent evaluation', scopes('cs.AI', 'cs.MA')),
    ('工具与规划', 'Tools and planning', scopes('cs.AI', 'cs.MA')),
    ('语言模型', 'Language models', scopes('cs.AI', 'cs.LG', 'cs.CL')),
    ('推理与对齐', 'Reasoning and alignment', scopes('cs.AI', 'cs.LG', 'cs.CL')),
    ('检索增强', 'Retrieval augmented generation', scopes('cs.AI', 'cs.CL')),
    ('持续学习', 'Continual learning', scopes('cs.LG')),
    ('具身智能', 'Embodied AI', scopes('cs.AI', 'cs.CV', 'cs.RO')),
    ('优化与泛化', 'Optimization and generalization', scopes('cs.LG', 'stat.ML')),
    ('表示学习', 'Representation learning', scopes('cs.LG', 'cs.CV')),
    ('生成模型', 'Generative models', scopes('cs.LG', 'cs.CV')),
    ('强化学习', 'Reinforcement learning', scopes('cs.LG', 'cs.RO')),
    ('图学习', 'Graph learning', scopes('cs.LG')),
    ('高效训练与推理', 'Efficient training and inference', scopes('cs.LG')),
    ('多语言处理', 'Multilingual NLP', scopes('cs.CL')),
    ('信息抽取', 'Information extraction', scopes('cs.CL')),
    ('图像理解', 'Image understanding', scopes('cs.CV')),
    ('多模态学习', 'Multimodal learning', scopes('cs.CV', 'cs.CL')),
    ('视频理解', 'Video understanding', scopes('cs.CV')),
    ('三维视觉', '3D vision', scopes('cs.CV')),
    ('多智能体协作', 'Multi-agent collaboration', scopes('cs.MA')),
    ('博弈与决策', 'Game theory and decision making', scopes('cs.MA')),
    ('进化算法', 'Evolutionary algorithms', scopes('cs.NE')),
    ('神经网络架构', 'Neural network architectures', scopes('cs.NE')),
    ('脉冲神经网络', 'Spiking neural networks', scopes('cs.NE')),
    ('机器人学习', 'Robot learning', scopes('cs.RO')),
    ('运动规划', 'Motion planning', scopes('cs.RO')),
    ('机器人操作', 'Robotic manipulation', scopes('cs.RO')),
    ('贝叶斯推断', 'Bayesian inference', scopes('stat.ML')),
    ('统计学习理论', 'Statistical learning theory', scopes('stat.ML')),
    ('因果推断', 'Causal inference', scopes('stat.ML')),
    ('概率模型', 'Probabilistic models', scopes('stat.ML')),
    ('图论与 Ramsey 理论', 'Graph coloring and Ramsey theory', scopes('math.CO', conferences=False)),
    ('极值组合', 'Extremal combinatorics', scopes('math.CO', conferences=False)),
    ('枚举组合', 'Enumerative combinatorics', scopes('math.CO', conferences=False)),
    ('概率组合', 'Probabilistic combinatorics', scopes('math.CO', conferences=False)),
    ('加性组合', 'Additive combinatorics', scopes('math.CO', conferences=False)),
    ('组合算法', 'Combinatorial algorithms', scopes('math.CO', conferences=False)),
    ('图结构', 'Graph structure', scopes('math.CO', conferences=False)),
]

TOPIC_DEFINITIONS = {
    'Agents': 'Agent architectures, autonomy and agent behavior; merely using an agent is insufficient.',
    'Memory and long context': 'Memory mechanisms and long-context processing for language models or agents.',
    'Agent evaluation': 'Benchmarks, protocols and metrics for autonomous agent capabilities; ordinary language/vision model benchmarks are excluded.',
    'Tools and planning': 'LLM/agent tool invocation and action planning; optimization schedules or tree search alone are insufficient.',
    'Language models': 'Language-model architectures, pretraining, adaptation and analysis.',
    'Reasoning and alignment': 'Language-model reasoning capabilities, preference alignment, safety or hallucination; ordinary application tasks/explanations are excluded.',
    'Retrieval augmented generation': 'Retrieving external evidence to ground generated answers.',
    'Continual learning': 'Learning sequential tasks or changing distributions while retaining earlier knowledge.',
    'Embodied AI': 'Perception-action intelligence interacting with physical environments; clinical/time-series analysis without physical actions is excluded.',
    'Optimization and generalization': 'Learning objectives, optimization algorithms and generalization behavior.',
    'Representation learning': 'Learning reusable feature representations, including self-supervised learning.',
    'Generative models': 'Models and methods for generating data, including diffusion, GANs and autoregressive generation.',
    'Reinforcement learning': 'Learning policies from rewards and interaction with an environment.',
    'Graph learning': 'Machine learning on graphs, including graph neural networks; excludes purely combinatorial graph theory.',
    'Efficient training and inference': 'Training/inference acceleration, compression, quantization, GPU kernels and resource efficiency.',
    'Multilingual NLP': 'Multilingual or cross-lingual language processing and language transfer.',
    'Information extraction': 'Extracting entities, relations, events or structured facts from text.',
    'Image understanding': 'Image recognition, detection, segmentation and visual understanding.',
    'Multimodal learning': 'Joint learning, reasoning or generation across multiple modalities.',
    'Video understanding': 'Video perception, temporal understanding and action recognition.',
    '3D vision': '3D reconstruction, geometry, point clouds and spatial scene understanding.',
    'Multi-agent collaboration': 'Coordination, communication and collaborative behavior among multiple agents.',
    'Game theory and decision making': 'Strategic interactions, equilibria and multi-agent decision making.',
    'Evolutionary algorithms': 'Evolutionary search, genetic algorithms and population-based optimization.',
    'Neural network architectures': 'Neural architecture design, architecture search and structural innovations.',
    'Spiking neural networks': 'Spike-based neural computation and learning.',
    'Robot learning': 'Learning robot perception, control and physical skills.',
    'Motion planning': 'Robot trajectory, path and collision-free motion planning.',
    'Robotic manipulation': 'Robotic grasping, object manipulation and dexterous interaction.',
    'Bayesian inference': 'Bayesian posterior inference, uncertainty estimation and probabilistic estimation.',
    'Statistical learning theory': 'Statistical foundations, sample complexity and learning guarantees.',
    'Causal inference': 'Causal identification, interventions, counterfactuals and causal estimation.',
    'Probabilistic models': 'Probabilistic modeling, latent variables and distributions for statistical learning.',
    'Graph coloring and Ramsey theory': 'Graph coloring, chromatic properties and Ramsey-type results; graph/packing/covering results without these are excluded.',
    'Extremal combinatorics': 'Extremal bounds and forbidden configurations in combinatorial structures.',
    'Enumerative combinatorics': 'Counting structures, generating functions, tableaux, bijections and exact enumeration; existence/construction alone is insufficient.',
    'Probabilistic combinatorics': 'Probabilistic methods, random graphs and random combinatorial structures.',
    'Additive combinatorics': 'Sumsets, additive structure and arithmetic combinatorial properties; using groups for constructions alone is insufficient.',
    'Combinatorial algorithms': 'Algorithms for discrete combinatorial problems and their complexity.',
    'Graph structure': 'Structural graph theory, connectivity, decompositions, graph classes and minors.',
}

# These former topic roots duplicate source categories rather than research topics.
RETIRED_SCOPES = {
    'Artificial intelligence': ['arxiv:cs.AI'],
    'Machine learning': ['arxiv:cs.LG', 'arxiv:stat.ML'],
    'Computer vision': ['arxiv:cs.CV'],
    'Mathematics': ['arxiv:math.CO'],
    'Combinatorics': ['arxiv:math.CO'],
}


def topic_keys(topic):
    return set(json.loads(topic.get('category_keys') or '[]')) & SUPPORTED_KEYS


def paper_keys(paper):
    if paper.get('venue'):
        return {'venue:' + code for code in VENUES if paper['venue'].split('.')[0].casefold()==code.casefold()}
    categories = json.loads(paper.get('categories') or '[]')
    keys = {'arxiv:' + code for code in categories}
    if paper.get('primary_category'):
        keys.add('arxiv:' + paper['primary_category'])
    return keys & SUPPORTED_KEYS


def scoped_topics(paper, topics):
    keys = paper_keys(paper)
    return [topic for topic in topics if topic_keys(topic) & keys]


def migrate_flat_topics(db):
    """One-time migration; preserve topic IDs, paper records and account history."""
    db.execute('CREATE TABLE IF NOT EXISTS app_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)')
    migration = 'flat_source_topics_v1'
    if db.execute('SELECT 1 FROM app_migrations WHERE name=?', (migration,)).fetchone():
        return
    old = {t['id']: dict(t) for t in db.execute('SELECT * FROM topics')}
    seeds = {en: keys for _, en, keys in TOPIC_SEEDS}
    existing = {t['name_en'] for t in old.values()}
    for zh, en, keys in TOPIC_SEEDS:
        if en not in existing:
            db.execute('INSERT INTO topics(name_zh,name_en,parent_id,category_keys,created_at) VALUES(?,?,NULL,?,?)',
                       (zh, en, json.dumps(keys), now()))
    retired = {t['id']: RETIRED_SCOPES[t['name_en']] for t in old.values() if t['name_en'] in RETIRED_SCOPES}
    for topic in old.values():
        if topic['id'] in retired:
            db.execute("UPDATE topics SET parent_id=NULL,status='merged',category_keys='[]' WHERE id=?", (topic['id'],))
            continue
        keys = seeds.get(topic['name_en'])
        if keys is None:
            inherited = topic_keys(topic)
            parent, visited = old.get(topic['parent_id']), {topic['id']}
            while parent and parent['id'] not in visited:
                visited.add(parent['id'])
                inherited.update(seeds.get(parent['name_en'], topic_keys(parent)))
                parent = old.get(parent['parent_id'])
            keys = sorted(inherited)
        db.execute('UPDATE topics SET parent_id=NULL,category_keys=? WHERE id=?', (json.dumps(keys), topic['id']))
    active = {t['id']: dict(t) for t in db.execute("SELECT * FROM topics WHERE status='active'")}
    # Detach obsolete or incompatible tags, and queue only affected papers for reclassification.
    papers = {p['id']: dict(p) for p in db.execute('SELECT id,primary_category,categories,venue FROM papers')}
    invalid = []
    for link in db.execute('SELECT paper_id,topic_id FROM paper_topics'):
        topic = active.get(link['topic_id'])
        if not topic or (topic['name_en'] != 'Uncategorized' and not topic_keys(topic) & paper_keys(papers[link['paper_id']])):
            invalid.append((link['paper_id'], link['topic_id']))
    db.executemany('DELETE FROM paper_topics WHERE paper_id=? AND topic_id=?', invalid)
    db.executemany('UPDATE papers SET classified=0 WHERE id=?', [(ident,) for ident in {p for p, _ in invalid}])

    def expand(ids):
        result = set(ids)
        while True:
            expanded = result | {t['id'] for t in old.values() if t['parent_id'] in result}
            if expanded == result:
                return result
            result = expanded

    # Convert former topic-root interests into source subscriptions in a new profile version.
    for profile in db.execute('SELECT * FROM interest_profile WHERE id IN (SELECT MAX(id) FROM interest_profile GROUP BY user_id)').fetchall():
        structured = json.loads(profile['structured'])
        original = json.dumps(structured, sort_keys=True)
        selection = structured.get('category_selection')
        categories = set(selection.get('categories', [])) & SUPPORTED_KEYS if selection else set()
        partial = {}
        if selection:
            for key, ids in selection.get('topics', {}).items():
                if key not in SUPPORTED_KEYS:
                    continue
                expanded = expand(ids)
                if any(key in retired.get(ident, []) for ident in expanded):
                    categories.add(key)
                partial[key] = sorted(ident for ident in expanded if ident in active and key in topic_keys(active[ident]))
        else:
            for ident in expand(structured.get('topic_ids', [])):
                categories.update(retired.get(ident, []))
                if ident in active:
                    for key in topic_keys(active[ident]):
                        partial.setdefault(key, []).append(ident)
        selection = {'categories': sorted(categories), 'topics': {key: sorted(set(ids)) for key, ids in partial.items() if ids and key not in categories}}
        structured['category_selection'] = selection
        structured['topic_ids'] = sorted({ident for ids in selection['topics'].values() for ident in ids} |
                                        {ident for ident, topic in active.items() if topic_keys(topic) & categories})
        if json.dumps(structured, sort_keys=True) != original:
            db.execute('INSERT INTO interest_profile(user_id,version,content,structured,embedding,change_reason,created_at) VALUES(?,?,?,?,?,?,?)',
                       (profile['user_id'], profile['version'] + 1, profile['content'], json.dumps(structured, ensure_ascii=False), profile['embedding'], 'taxonomy_flatten', now()))
    db.execute('INSERT INTO app_migrations VALUES(?,?)', (migration, now()))

TOPICS = [
 ('人工智能', 'Artificial intelligence', None),
 ('智能体', 'Agents', 1), ('记忆与长上下文', 'Memory and long context', 2),
 ('智能体评测', 'Agent evaluation', 2), ('工具与规划', 'Tools and planning', 2),
 ('语言模型', 'Language models', 1), ('推理与对齐', 'Reasoning and alignment', 6),
 ('检索增强', 'Retrieval augmented generation', 6),
 ('机器学习', 'Machine learning', None), ('持续学习', 'Continual learning', 9),
 ('计算机视觉', 'Computer vision', 9), ('具身智能', 'Embodied AI', 9),
 ('数学', 'Mathematics', None), ('组合数学', 'Combinatorics', 13),
 ('图论与 Ramsey 理论', 'Graph coloring and Ramsey theory', 14),
 ('未分类', 'Uncategorized', None),
]
