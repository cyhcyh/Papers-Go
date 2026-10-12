"""Bounded chat history, durable confirmation states and cross-process activity leases."""
import json
from datetime import datetime, timedelta

from ..config import now
from ..db import connect, dumps

MESSAGE_DAYS = 365
MESSAGE_LIMIT = 50_000
EMPTY_DAYS = 7
EMPTY_LIMIT = 100


def initialize(db):
    columns = {r['name'] for r in db.execute('PRAGMA table_info(chat_sessions)')}
    for name in ('active_until',):
        if name not in columns:
            db.execute(f'ALTER TABLE chat_sessions ADD COLUMN {name} TEXT')
    if 'message_id' not in {r['name'] for r in db.execute('PRAGMA table_info(pending_tools)')}:
        db.execute('ALTER TABLE pending_tools ADD COLUMN message_id INTEGER REFERENCES chat_messages(id) ON DELETE CASCADE')
    db.executescript('''
    CREATE INDEX IF NOT EXISTS idx_chat_user_sessions ON chat_sessions(user_id,id DESC);
    CREATE INDEX IF NOT EXISTS idx_chat_sessions_date ON chat_sessions(created_at,id);
    CREATE INDEX IF NOT EXISTS idx_chat_messages_date ON chat_messages(created_at,id);
    CREATE INDEX IF NOT EXISTS idx_chat_messages_cursor ON chat_messages(session_id,created_at,id);
    CREATE INDEX IF NOT EXISTS idx_chat_messages_role ON chat_messages(session_id,role,id);
    CREATE INDEX IF NOT EXISTS idx_pending_tools_date ON pending_tools(created_at,id);
    CREATE INDEX IF NOT EXISTS idx_pending_tools_session ON pending_tools(session_id,id);
    CREATE INDEX IF NOT EXISTS idx_pending_tools_message ON pending_tools(message_id);
    CREATE TABLE IF NOT EXISTS chat_context (
      session_id INTEGER PRIMARY KEY REFERENCES chat_sessions(id) ON DELETE CASCADE,
      summary TEXT NOT NULL,through_id INTEGER NOT NULL,through_at TEXT NOT NULL,
      revision INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL);
    ''')


def touch(session_id, user_id, active=True):
    stamp = now()
    with connect() as db:
        db.execute('UPDATE chat_sessions SET active_until=? WHERE id=? AND user_id=?',
                   ((datetime.fromisoformat(stamp)+timedelta(minutes=10)).isoformat() if active else None, session_id, user_id))


def proposal_status(db, session_id, identifier, status, message_id=None):
    if message_id:
        records = db.execute('SELECT id,tool_calls FROM chat_messages WHERE id=? AND session_id=?', (message_id, session_id)).fetchall()
    else:
        records = db.execute('''SELECT id,tool_calls FROM chat_messages WHERE session_id=? AND role='assistant'
            AND tool_calls IS NOT NULL AND EXISTS(SELECT 1 FROM json_each(
              CASE WHEN json_valid(tool_calls) THEN tool_calls ELSE '[]' END)
              WHERE json_extract(value,'$.id')=?)''', (session_id, identifier)).fetchall()
    for record in records:
        payload = json.loads(record['tool_calls'] or '[]')
        for item in payload:
            if item.get('id') == identifier:
                item['status'] = status
        db.execute('UPDATE chat_messages SET tool_calls=? WHERE id=?', (dumps(payload), record['id']))


def clear_session(db, session_id):
    db.execute('DELETE FROM pending_tools WHERE session_id=?', (session_id,))
    db.execute('DELETE FROM chat_messages WHERE session_id=?', (session_id,))
    db.execute('DELETE FROM chat_sessions WHERE id=?', (session_id,))


def prune_turn(db, message, cutoff=None):
    """Remove one complete old turn. Never detach a tool result from its turn."""
    sid = message['session_id']
    boundary = db.execute("SELECT id FROM chat_messages WHERE session_id=? AND role='user' AND id>? ORDER BY id LIMIT 1",
                          (sid, message['id'])).fetchone()
    condition = 'session_id=?' + (' AND id<?' if boundary else '')
    args = (sid, boundary['id']) if boundary else (sid,)
    if cutoff and db.execute('SELECT 1 FROM chat_messages WHERE '+condition+' AND created_at>=? LIMIT 1', (*args, cutoff)).fetchone():
        return 0
    count = db.execute('DELETE FROM chat_messages WHERE '+condition, args).rowcount
    # Legacy proposals may not have a message_id yet.
    db.execute('''DELETE FROM pending_tools WHERE session_id=? AND message_id IS NULL
        AND created_at<?''', (sid, message['created_at']))
    if not db.execute('SELECT 1 FROM chat_messages WHERE session_id=? LIMIT 1', (sid,)).fetchone():
        clear_session(db, sid)
    return count


def maintain(stamp, batch_size=500):
    cutoff = (datetime.fromisoformat(stamp)-timedelta(days=MESSAGE_DAYS)).isoformat()
    removed = 0
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        candidates = db.execute('''SELECT m.id,m.session_id,m.created_at FROM chat_messages m
            JOIN chat_sessions s ON s.id=m.session_id WHERE m.created_at<?
            AND (s.active_until IS NULL OR s.active_until<?) ORDER BY m.created_at,m.id LIMIT ?''',
            (cutoff, stamp, batch_size)).fetchall()
        for message in candidates:
            if not db.execute('SELECT 1 FROM chat_messages WHERE id=? AND session_id=?',(message['id'],message['session_id'])).fetchone():
                continue
            removed += prune_turn(db, message, cutoff)
            if removed >= batch_size:
                return removed
        position = db.execute("SELECT item FROM retention_cursors WHERE name='chat_users'").fetchone()
        users = db.execute('SELECT DISTINCT user_id AS id FROM chat_sessions WHERE user_id>? ORDER BY user_id LIMIT 5', (int(position[0]) if position else 0,)).fetchall()
        for user in users:
            excess=db.execute('''SELECT s.id FROM chat_sessions s WHERE s.user_id=?
                AND (s.active_until IS NULL OR s.active_until<?)
                AND NOT EXISTS(SELECT 1 FROM chat_messages m WHERE m.session_id=s.id)
                ORDER BY s.id DESC LIMIT ? OFFSET ?''',(user['id'],stamp,max(0,batch_size-removed),EMPTY_LIMIT)).fetchall()
            for session in excess:clear_session(db,session['id'])
            removed+=len(excess)
            count = db.execute('SELECT COUNT(*) FROM chat_messages m JOIN chat_sessions s ON s.id=m.session_id WHERE s.user_id=?', (user['id'],)).fetchone()[0]
            if count > MESSAGE_LIMIT and removed < batch_size:
                eligible = db.execute('''SELECT m.id,m.session_id,m.created_at FROM chat_messages m
                    JOIN chat_sessions s ON s.id=m.session_id WHERE s.user_id=?
                    AND (s.active_until IS NULL OR s.active_until<?) ORDER BY m.created_at,m.id LIMIT ?''',
                    (user['id'], stamp,batch_size-removed)).fetchall()
                for message in eligible:
                    if count<=MESSAGE_LIMIT or removed>=batch_size:break
                    if not db.execute('SELECT 1 FROM chat_messages WHERE id=? AND session_id=?',(message['id'],message['session_id'])).fetchone():continue
                    amount = prune_turn(db, message)
                    count -= amount
                    removed += amount
            if removed >= batch_size:
                break
        # Repeat a user with a quota backlog instead of losing its place.
        cursor = user['id']-1 if users and removed >= batch_size else users[-1]['id'] if len(users)==5 else 0
        db.execute("INSERT INTO retention_cursors VALUES('chat_users',?,?) ON CONFLICT(name) DO UPDATE SET stamp=excluded.stamp,item=excluded.item", (stamp, str(cursor)))
        empty_cutoff = (datetime.fromisoformat(stamp)-timedelta(days=EMPTY_DAYS)).isoformat()
        position=db.execute("SELECT stamp,item FROM retention_cursors WHERE name='empty_chats'").fetchone()
        checkpoint=tuple(position) if position else ('','0')
        available=max(0,batch_size-removed)
        scanned=db.execute('''SELECT id,created_at,active_until FROM chat_sessions WHERE created_at<?
            AND (created_at,id)>(?,?) ORDER BY created_at,id LIMIT ?''',
            (empty_cutoff,checkpoint[0],int(checkpoint[1]),available)).fetchall()
        for item in scanned:
            if (not item['active_until'] or item['active_until']<stamp) and not db.execute('SELECT 1 FROM chat_messages WHERE session_id=? LIMIT 1',(item['id'],)).fetchone():
                clear_session(db,item['id']);removed+=1
        more=available>0 and len(scanned)==available
        checkpoint=(scanned[-1]['created_at'],str(scanned[-1]['id'])) if more else ('','0')
        db.execute("INSERT INTO retention_cursors VALUES('empty_chats',?,?) ON CONFLICT(name) DO UPDATE SET stamp=excluded.stamp,item=excluded.item",checkpoint)
    return batch_size if len(users)==5 or more else removed
