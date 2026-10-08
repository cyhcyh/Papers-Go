import pytest

from app.config import now
from app.db import connect, execute, one
from .conftest import headers


@pytest.fixture
def notices(accounts):
    alice, bob = accounts
    def insert(account, count):
        return [execute('INSERT INTO notifications(user_id,type,title,body,read,created_at) VALUES(?,?,?,?,?,?)',
            (account['user']['id'], 'watch_hit' if i % 2 else 'collision', f'Notice {i}', 'Full notification text', i % 3 == 0, now())) for i in range(count)]
    return alice, bob, insert(alice, 225), insert(bob, 3)


def test_server_pagination_and_filters(client, notices):
    alice, _, ids, _ = notices
    auth = headers(alice)
    first = client.get('/api/notifications', headers=auth).json()
    assert first['total'] == 225 and first['limit'] == 20 and first['offset'] == 0
    assert [n['id'] for n in first['items']] == list(reversed(ids))[:20]
    assert first['unread'] == 150
    second = client.get('/api/notifications?offset=20&limit=20', headers=auth).json()
    assert [n['id'] for n in second['items']] == list(reversed(ids))[20:40]
    last = client.get('/api/notifications?offset=220', headers=auth).json()
    assert len(last['items']) == 5
    filtered = client.get('/api/notifications?status=read&type=collision', headers=auth).json()
    assert filtered['total'] == 38 and filtered['unread'] == 150
    assert all(n['read'] and n['type'] == 'collision' for n in filtered['items'])
    unread = client.get('/api/notifications?status=unread', headers=auth).json()
    legacy = client.get('/api/notifications?unread=true', headers=auth).json()
    assert unread == legacy and unread['total'] == 150


def test_batch_management_is_scoped_and_deduplicated(client, notices, papers):
    alice, bob, ids, foreign = notices
    auth = headers(alice)
    targets = [ids[1], ids[2], ids[1], foreign[1], 9999999]
    response = client.patch('/api/notifications/batch', headers=auth, json={'ids': targets, 'read': True})
    assert response.status_code == 200 and response.json() == {'updated': 2}
    assert one('SELECT read FROM notifications WHERE id=?', (foreign[1],))['read'] == 0
    response = client.patch('/api/notifications/batch', headers=auth, json={'ids': targets, 'read': False})
    assert response.json() == {'updated': 2}
    execute('UPDATE notifications SET paper_id=? WHERE id=?', (papers[0], ids[1]))
    response = client.request('DELETE', '/api/notifications/batch', headers=auth, json={'ids': targets})
    assert response.status_code == 200 and response.json() == {'deleted': 2}
    assert one('SELECT id FROM papers WHERE id=?', (papers[0],))
    assert one('SELECT id FROM notifications WHERE id=?', (foreign[1],))
    assert client.get('/api/notifications', headers=auth).json()['total'] == 223
    assert client.get('/api/notifications', headers=headers(bob)).json()['total'] == 3
    assert client.post(f'/api/notifications/{foreign[0]}/read', headers=auth).status_code == 404
    assert client.post(f'/api/notifications/{ids[3]}/read', headers=auth).status_code == 200
    assert client.get('/api/notifications/count', headers=auth).json()['unread'] == 148


@pytest.mark.parametrize('method', ['PATCH', 'DELETE'])
@pytest.mark.parametrize('ids', [[], list(range(101))])
def test_invalid_batch_rejected(client, accounts, method, ids):
    assert client.request(method, '/api/notifications/batch', headers=headers(accounts[0]), json={'ids': ids, 'read': True}).status_code == 422


@pytest.mark.parametrize('query', ['offset=-1', 'limit=0', 'limit=101', 'status=other'])
def test_invalid_page_rejected(client, accounts, query):
    assert client.get('/api/notifications?' + query, headers=headers(accounts[0])).status_code == 422


def test_notification_indexes_avoid_sorting_all_rows(client, notices):
    alice = notices[0]['user']['id']
    with connect() as db:
        for condition, args in [('user_id=?', (alice,)), ('user_id=? AND read=?', (alice, 0))]:
            plan = [row['detail'] for row in db.execute('EXPLAIN QUERY PLAN SELECT * FROM notifications WHERE '+condition+' ORDER BY id DESC LIMIT 20 OFFSET 20', args)]
            assert any('INDEX' in entry for entry in plan)
            assert not any('TEMP B-TREE' in entry for entry in plan)


def test_notification_endpoints_require_login(client):
    assert client.get('/api/notifications').status_code == 401
    assert client.patch('/api/notifications/batch', json={'ids': [1], 'read': True}).status_code == 401
    assert client.request('DELETE', '/api/notifications/batch', json={'ids': [1]}).status_code == 401
