"""Bound automatic failures per paper and function, independently of model-call retries."""
from functools import wraps
from ..config import now
from ..db import connect, one, rows
from ..pipeline_control import check_cancelled

LIMIT = 3  # Initial processing and two later attempts.
FEATURES = {'classify':'classify', 'tldr_gen':'brief', 'assess_quality':'quality',
            'build_vectors':'embedding', 'preread':'reading_l2'}
COMPONENTS = {'classify':'classification', 'tldr_gen':'brief', 'assess_quality':'quality',
              'build_vectors':'embedding', 'preread':'reading'}


def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS paper_task_failures (
        task TEXT NOT NULL, paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
        failures INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
        PRIMARY KEY(task,paper_id));
        CREATE INDEX IF NOT EXISTS idx_paper_task_paused ON paper_task_failures(task,failures,paper_id);
        CREATE TRIGGER IF NOT EXISTS paper_task_material_changed AFTER UPDATE OF
            title,abstract,arxiv_version,primary_category,categories,venue,venue_rank,pdf_url ON papers
        WHEN NEW.title IS NOT OLD.title OR NEW.abstract IS NOT OLD.abstract
            OR NEW.arxiv_version IS NOT OLD.arxiv_version OR NEW.pdf_url IS NOT OLD.pdf_url
            OR NEW.primary_category IS NOT OLD.primary_category OR NEW.categories IS NOT OLD.categories
            OR NEW.venue IS NOT OLD.venue OR NEW.venue_rank IS NOT OLD.venue_rank
        BEGIN
            DELETE FROM paper_task_failures WHERE paper_id=NEW.id AND (
                NEW.title IS NOT OLD.title OR NEW.abstract IS NOT OLD.abstract
                OR NEW.arxiv_version IS NOT OLD.arxiv_version
                OR task IN ('classify','assess_quality')
                OR (task='preread' AND NEW.pdf_url IS NOT OLD.pdf_url));
        END;''')


def eligible(task, paper_id='id'):
    # Identifiers are internal constants, never request input.
    assert task in FEATURES
    return f"{paper_id} NOT IN (SELECT paper_id FROM paper_task_failures WHERE task='{task}' AND failures>={LIMIT})"


class PausedError(ValueError):
    pass


def check(task, paper_id):
    value=one('SELECT failures FROM paper_task_failures WHERE task=? AND paper_id=?',(task,paper_id))
    if value and value['failures']>=LIMIT:
        raise PausedError('该论文已连续失败 3 轮，自动处理已暂停，请使用“重试失败项”恢复')
    return value


def succeeded(task, paper_ids):
    with connect() as db:
        db.executemany('DELETE FROM paper_task_failures WHERE task=? AND paper_id=?',[(task,i) for i in paper_ids])


def failed(task, paper_ids):
    # Missing setup is not a failed processing round. This check never calls a model.
    from ..llm import runtime as models
    from ..task_readiness import DEPENDENCIES
    from .. import prompts
    if any(models.configuration_issue(f) for f in DEPENDENCIES[task]):return
    if task=='assess_quality' and not prompts.skill_enabled('quality'):return
    with connect() as db:
        db.executemany('''INSERT INTO paper_task_failures(task,paper_id,failures,updated_at)
            SELECT ?,id,1,? FROM papers WHERE id=?
            ON CONFLICT(task,paper_id) DO UPDATE SET failures=MIN(failures+1,?),updated_at=excluded.updated_at''',
            [(task,now(),i,LIMIT) for i in paper_ids])


def tracked(task):
    def decorate(function):
        @wraps(function)
        async def wrapped(paper,*args,**kwargs):
            check_cancelled()
            previous=check(task,paper['id'])
            try:
                result=await function(paper,*args,**kwargs)
            except Exception:
                check_cancelled()  # A transport may have absorbed cancellation.
                failed(task,[paper['id']])
                raise
            if previous:succeeded(task,[paper['id']])
            return result
        return wrapped
    return decorate


def snapshot():
    return {v['task']:{'failed':v['failed'],'paused':v['paused'],'limit':LIMIT} for v in rows(
        'SELECT task,COUNT(*) failed,SUM(failures>=?) paused FROM paper_task_failures WHERE failures>0 GROUP BY task',(LIMIT,))}


def create_retry_run(db, task):
    """Called inside the same transaction that queues the work and resets its budget."""
    from fastapi import HTTPException
    from ..db import dumps
    from ..llm import runtime as models
    if task not in FEATURES:raise HTTPException(404,'此任务不支持重试失败项')
    if not db.execute('SELECT 1 FROM paper_task_failures WHERE task=? LIMIT 1',(task,)).fetchone():
        raise HTTPException(409,'没有需要重试的失败论文')
    options={'mode':'retry','components':[COMPONENTS[task]]}
    if task=='build_vectors':options['embedding_identity']=models.embedding_identity(models.configuration())
    ident=db.execute('INSERT INTO pipeline_redo_runs(name,options,created_at,updated_at) VALUES(?,?,?,?)',
                     (task,dumps(options),now(),now())).lastrowid
    db.execute('''INSERT INTO pipeline_redo_items(run_id,paper_id,component)
        SELECT ?,paper_id,? FROM paper_task_failures WHERE task=?''',(ident,COMPONENTS[task],task))
    db.execute('DELETE FROM paper_task_failures WHERE task=?',(task,))
    return ident
