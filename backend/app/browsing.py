"""Per-user browsing state; history references live papers and never extends life."""
from datetime import datetime, timedelta
from collections import deque

from .config import now

COOLDOWN_DAYS = 7


def cutoff():
    return (datetime.fromisoformat(now()) - timedelta(days=COOLDOWN_DAYS)).isoformat()


def initialize(db):
    columns = {r['name'] for r in db.execute('PRAGMA table_info(user_paper_state)')}
    for name, definition in [('last_browsed_at', 'TEXT'), ('dismissed', 'INTEGER NOT NULL DEFAULT 0')]:
        if name not in columns:
            db.execute('ALTER TABLE user_paper_state ADD COLUMN ' + name + ' ' + definition)
    db.execute('CREATE INDEX IF NOT EXISTS idx_state_browsed ON user_paper_state(user_id,last_browsed_at DESC,paper_id DESC)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_interactions_user_paper ON interactions(user_id,paper_id,action,created_at DESC)')
    name = 'browsing-cooldown-v1'
    if db.execute('SELECT 1 FROM app_migrations WHERE name=?', (name,)).fetchone():
        return
    db.execute('''UPDATE user_paper_state SET seen=1,last_browsed_at=COALESCE(
        (SELECT MAX(i.created_at) FROM interactions i WHERE i.user_id=user_paper_state.user_id
         AND i.paper_id=user_paper_state.paper_id AND i.action IN ('view','skip','like','save')
         AND NOT EXISTS(SELECT 1 FROM interactions u WHERE u.target_id=i.id AND u.action='undo')),
        updated_at,?), dismissed=EXISTS(
        SELECT 1 FROM interactions i WHERE i.user_id=user_paper_state.user_id
        AND i.paper_id=user_paper_state.paper_id AND i.action='skip'
        AND NOT EXISTS(SELECT 1 FROM interactions u WHERE u.target_id=i.id AND u.action='undo'))
        WHERE seen=1 OR liked=1 OR saved=1''', (now(),))
    db.execute('INSERT INTO app_migrations VALUES(?,?)', (name, now()))


def states(db, user_id, ids):
    if user_id is None or not ids:
        return {}
    import json
    return {r['paper_id']: dict(r) for r in db.execute('''SELECT paper_id,liked,saved,dismissed,last_browsed_at
        FROM user_paper_state WHERE user_id=? AND paper_id IN (SELECT value FROM json_each(?))
        AND (seen=1 OR liked=1 OR saved=1 OR dismissed=1)''', (user_id,json.dumps(ids)))}


def returning(state, before):
    return bool(state['last_browsed_at'] and state['last_browsed_at'] <= before
                and not (state['liked'] or state['saved'] or state['dismissed']))


def mix(unread, returned, scores, categories=None, weights=None, consumed=None, *, previous_returns=(), presented=0, priorities=None):
    """Prioritize unread; at most two repeats in any twenty-position window."""
    categories, weights, consumed = categories or {}, weights or {None: 1}, consumed or {}
    unread_groups, return_groups = {}, {}
    for ids, groups in [(unread, unread_groups), (returned, return_groups)]:
        for ident in ids:
            key = categories.get(ident)
            groups.setdefault(key, deque()).append(ident)
    keys = list(dict.fromkeys([*unread_groups, *return_groups]))
    total_weight = sum(weights.get(key, 1) for key in keys)
    displayed = sum(consumed.get(key, 0) for key in keys)
    credit = {key: displayed * weights.get(key, 1) - consumed.get(key, 0) * total_weight for key in keys}
    counts, previous_priority = dict(consumed), None
    priorities = priorities or {}
    output, positions = [], deque(previous_returns)
    while keys:
        position = len(output)
        while positions and positions[0] <= position - 20:
            positions.popleft()
        has_unread = any(unread_groups.get(key) for key in keys)
        priority = min((priorities.get(group[0],1) for group in unread_groups.values() if group),default=1)
        if previous_priority is not None and priority != previous_priority:
            total_weight = sum(weights.get(key,1) for key in keys)
            displayed = sum(counts.get(key,0) for key in keys)
            credit = {key: displayed * weights.get(key,1) - counts.get(key,0) * total_weight for key in keys}
        previous_priority = priority
        allowed = len(positions) < 2 and (not has_unread or presented + position >= 9 and
                  (not positions or position - positions[-1] >= 10))
        ready = {key:group for key,group in unread_groups.items() if group and priorities.get(group[0],1)==priority}
        available = [key for key in keys if ready.get(key) or allowed and return_groups.get(key)]
        if not available:
            break
        total_weight = sum(weights.get(key, 1) for key in available)
        for key in available:
            credit[key] += weights.get(key, 1)
        key = max(available, key=lambda key: credit[key])
        credit[key] -= total_weight
        new, old = ready.get(key), return_groups.get(key)
        if old and allowed and (not new or scores[old[0]] > scores[new[0]]):
            output.append(old.popleft()); positions.append(position)
        else:
            output.append(new.popleft())
        counts[key] = counts.get(key,0) + 1
        if not unread_groups.get(key) and not old:
            keys.remove(key)
    return output
