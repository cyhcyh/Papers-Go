"""One lightweight quality formula shared by assessment and cached signal updates."""
import json
import math

from ..config import now

VERSION = 'quality-v3'
NEUTRAL = 50.
FIELDS = {'cs': '计算机科学', 'math': '数学', 'stat': '统计学', 'physics': '物理学',
          'eess': '电气工程与系统科学', 'econ': '经济学', 'q-bio': '定量生物学', 'q-fin': '定量金融'}


def discipline(paper):
    primary = (paper.get('primary_category') or '').casefold()
    if primary.startswith('math.'):
        return '数学', 'quality'
    if primary.startswith('cs.') or primary == 'stat.ml' or paper.get('venue'):
        return '计算机科学', 'quality'
    prefix = primary.split('.')[0]
    label = FIELDS.get(prefix, '物理学' if prefix in ('quant-ph', 'hep-ph', 'hep-th', 'cond-mat', 'astro-ph', 'gr-qc') else '其他学科')
    return label, 'quality'


def bounded(value, default=0.):
    if value is None:
        return default
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('评分必须是有限数值')
    return max(0., min(100., number))


def community_score(paper):
    votes = max(0, paper.get('hf_upvotes') or 0)
    stars = max(0, paper.get('github_stars') or 0)
    return min(100., votes * 2 + math.log1p(stars) * 10)


def components(paper, assessment):
    if not has_content_score(assessment):
        return {'content': None, 'venue': None, 'base': None,
                'author_bonus': 0., 'community_bonus': 0., 'total': None}
    content = bounded(assessment.get('contribution', assessment.get('novelty')))
    # Unknown presentation level remains unknown, while confirmed acceptance is a signal.
    venue = {'oral': 95., 'spotlight': 80., 'poster': 60.}.get((paper.get('venue_rank') or '').casefold())
    if venue is None and paper.get('venue'):
        venue = 60.
    base = content if venue is None else .8 * content + .2 * venue
    author = bounded(paper.get('author_impact')) * .03
    community = community_score(paper) * .02
    return {'content': round(content, 2), 'venue': venue, 'base': round(base, 2),
            'author_bonus': round(author, 2), 'community_bonus': round(community, 2),
            'total': round(min(100., base + author + community), 2)}


def has_content_score(assessment):
    return assessment.get('contribution', assessment.get('novelty')) is not None


def quality_score(paper, assessment=None):
    return components(paper, assessment or {})['total']


def recompute(db, paper_id):
    row = db.execute('''SELECT scored,skeleton,base_quality_score,community_bonus,quality_score,
        venue_rank,venue,author_impact,hf_upvotes,github_stars FROM papers WHERE id=?''', (paper_id,)).fetchone()
    if row is None:
        return None
    paper = dict(row)
    if not paper['scored']:
        base, community, total = None, 0., NEUTRAL
    elif paper.get('skeleton'):
        value = components(paper, json.loads(paper['skeleton']))
        base, community, total = value['base'], value['community_bonus'], value['total']
    else:
        # Retain an existing assessment even if an old record lacks its JSON details.
        base = paper['base_quality_score']
        if base is None:
            base = paper['quality_score'] if paper['quality_score'] is not None else NEUTRAL
        community = round(community_score(paper) * .02, 2)
        total = round(min(100., base + bounded(paper.get('author_impact')) * .03 + community), 2)
    if (paper['base_quality_score'], paper.get('community_bonus'), paper['quality_score']) != (base, community, total):
        db.execute('UPDATE papers SET base_quality_score=?,community_bonus=?,quality_score=? WHERE id=?',
                   (base, community, total, paper_id))
    return total


def initialize(db):
    if 'community_bonus' not in {r['name'] for r in db.execute('PRAGMA table_info(papers)')}:
        db.execute('ALTER TABLE papers ADD COLUMN community_bonus REAL NOT NULL DEFAULT 0')
    if not db.execute('SELECT 1 FROM app_migrations WHERE name=?', ('quality-v2',)).fetchone():
        db.execute('UPDATE papers SET quality_score=50,base_quality_score=NULL,community_bonus=0 WHERE scored=0')
        for row in db.execute('SELECT id FROM papers WHERE scored=1').fetchall():
            recompute(db, row['id'])
        db.execute('INSERT INTO app_migrations VALUES(?,?)', ('quality-v2', now()))
    if not db.execute('SELECT 1 FROM app_migrations WHERE name=?', ('quality-v2.1',)).fetchone():
        # Clear only fabricated neutral scores; keep completed assessments and old valid results.
        db.execute('''UPDATE papers SET quality_score=NULL,base_quality_score=NULL,community_bonus=0
            WHERE scored=1 AND skeleton IS NOT NULL
            AND json_extract(skeleton,'$.contribution') IS NULL AND json_extract(skeleton,'$.novelty') IS NULL
            AND json_extract(skeleton,'$.evidence') IS NULL AND json_extract(skeleton,'$.rigor') IS NULL''')
        db.execute('INSERT INTO app_migrations VALUES(?,?)', ('quality-v2.1', now()))
    if not db.execute('SELECT 1 FROM app_migrations WHERE name=?', (VERSION,)).fetchone():
        # Reuse saved contribution judgments; leave completion flags, JSON and vectors intact.
        # Legacy records without assessment details cannot be reweighted reliably.
        for row in db.execute('SELECT id FROM papers WHERE scored=1 AND skeleton IS NOT NULL').fetchall():
            recompute(db, row['id'])
        db.execute('INSERT INTO app_migrations VALUES(?,?)', (VERSION, now()))
    db.executescript('''CREATE TRIGGER IF NOT EXISTS pending_quality_insert AFTER INSERT ON papers
        WHEN NEW.scored=0 BEGIN
        UPDATE papers SET quality_score=50,base_quality_score=NULL,community_bonus=0 WHERE id=NEW.id;
        END;
        CREATE TRIGGER IF NOT EXISTS pending_quality_update AFTER UPDATE OF scored ON papers
        WHEN NEW.scored=0 AND OLD.scored!=0 BEGIN
        UPDATE papers SET quality_score=50,base_quality_score=NULL,community_bonus=0 WHERE id=NEW.id;
        END;
        DROP TRIGGER IF EXISTS author_match_reset;
        CREATE TRIGGER author_match_reset AFTER UPDATE OF title,authors ON papers
        WHEN NEW.title!=OLD.title OR NEW.authors!=OLD.authors BEGIN
        DELETE FROM author_work_matches WHERE paper_id=NEW.id;
        DELETE FROM paper_author_links WHERE paper_id=NEW.id;
        UPDATE papers SET author_impact=0,quality_score=CASE WHEN NEW.scored=0 THEN 50
            WHEN NEW.base_quality_score IS NULL AND NEW.quality_score IS NULL THEN NULL
            ELSE MIN(100,COALESCE(base_quality_score,50)+community_bonus) END WHERE id=NEW.id;
        END;''')
