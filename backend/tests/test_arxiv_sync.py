import json
import httpx
import pytest
from app.config import settings
from app.db import execute, one
from app.pipeline import fetch


def atom(identities, start=0, total=None):
    entries = ''.join(f'''<entry><id>https://arxiv.org/abs/2609.{identity:05d}</id>
      <title>Paper {identity}</title><summary>Abstract</summary><published>2026-09-30T00:00:00Z</published>
      <x:primary_category term="cs.AI"/><category term="cs.AI"/><category term="math.CO"/></entry>'''
      for identity in identities)
    return f'''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:x="http://arxiv.org/schemas/atom"
      xmlns:o="http://a9.com/-/spec/opensearch/1.1/">
      <o:startIndex>{start}</o:startIndex><o:totalResults>{total if total is not None else len(identities)}</o:totalResults>{entries}</feed>'''


def install_mock(monkeypatch, handler):
    from app.pipeline import arxiv_daily
    async def daily_metadata_only(**kwargs):return None
    # These fixtures describe the Atom synchronization protocol only. RSS
    # announcement validation has separate complete/partial batch tests.
    monkeypatch.setattr(arxiv_daily,'sync_latest',daily_metadata_only)
    original_client = httpx.AsyncClient
    monkeypatch.setattr(fetch.httpx, 'AsyncClient', lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    async def no_wait(_): pass
    monkeypatch.setattr(fetch.asyncio, 'sleep', no_wait)
    monkeypatch.setattr(fetch, 'now', lambda: '2026-10-01T12:00:00+00:00')
    settings().arxiv_categories = 'cs.AI,math.CO'
    execute("UPDATE source_categories SET fetch_enabled=(key IN ('arxiv:cs.AI','arxiv:math.CO')) WHERE kind='arxiv'")
    from app.source_catalog import invalidate
    invalidate()
    settings().fetch_limit = 1  # Must not affect a daily or unrestricted manual run.


@pytest.mark.asyncio
async def test_daily_sync_fetches_all_pages_and_deduplicates_cross_listings(client, monkeypatch):
    requests = []
    def handler(request):
        query = request.url.params['search_query']
        start = int(request.url.params['start'])
        requests.append((query, start))
        assert int(request.url.params['max_results']) == 500
        assert request.url.params['sortOrder'] == 'ascending'
        if 'submittedDate:[202609300000 TO 202610010000]' in query:
            assert 'cat:cs.AI OR cat:math.CO' in query
            ids = list(range(1, 1233))
            return httpx.Response(200, text=atom(ids[start:start+int(request.url.params['max_results'])], start, len(ids)))
        return httpx.Response(200, text=atom([]))
    install_mock(monkeypatch, handler)
    assert await fetch.fetch_arxiv() == 1232
    assert one('SELECT COUNT(*) AS n FROM papers')['n'] == 1232
    assert [offset for query, offset in requests if 'submittedDate:[202609300000 TO 202610010000]' in query] == [0, 500, 1000]
    assert json.loads(one("SELECT categories FROM papers WHERE arxiv_id='2609.00001'")['categories']) == ['cs.AI', 'math.CO']
    for category in ('cs.AI', 'math.CO'):
        cursor = one('SELECT * FROM arxiv_cursors WHERE category=?', (category,))
        assert cursor['first_sync_from'] == '2026-09-24T00:00:00+00:00'
        assert cursor['synced_through'] == '2026-10-01T12:00:00+00:00'


@pytest.mark.asyncio
async def test_failed_category_is_backfilled_while_other_category_finishes(client, monkeypatch):
    execute('INSERT INTO arxiv_cursors VALUES(?,?,?)',('math.CO','2026-10-01T00:00:00+00:00','2026-10-01T00:00:00+00:00'))
    failing = True
    requests = []
    def handler(request):
        query = request.url.params['search_query']
        start = int(request.url.params['start'])
        requests.append((query, start))
        if 'submittedDate:[202609300000 TO 202610010000]' in query:
            if 'cat:cs.AI' in query:
                if failing and start == 500:
                    return httpx.Response(502)
                ids = list(range(1, 1231))
            return httpx.Response(200, text=atom(ids[start:start+int(request.url.params['max_results'])], start, len(ids)))
        return httpx.Response(200, text=atom([1231] if 'cat:math.CO' in query else []))
    install_mock(monkeypatch, handler)
    with pytest.raises(RuntimeError, match='cs.AI'):
        await fetch.fetch_arxiv()
    assert one("SELECT synced_through FROM arxiv_cursors WHERE category='cs.AI'")['synced_through'] == '2026-09-30T00:00:00+00:00'
    assert one("SELECT synced_through FROM arxiv_cursors WHERE category='math.CO'")['synced_through'] == '2026-10-01T12:00:00+00:00'
    assert one('SELECT COUNT(*) AS n FROM papers')['n'] == 501
    failing = False
    requests.clear()
    assert await fetch.fetch_arxiv() == 730
    assert one('SELECT COUNT(*) AS n FROM papers')['n'] == 1231
    assert any('202609300000 TO 202610010000' in query and offset == 500 for query, offset in requests)
    assert one("SELECT synced_through FROM arxiv_cursors WHERE category='cs.AI'")['synced_through'] == '2026-10-01T12:00:00+00:00'


@pytest.mark.asyncio
async def test_sync_overlap_collects_late_published_entries(client, monkeypatch):
    for category in ('cs.AI', 'math.CO'):
        execute('INSERT INTO arxiv_cursors VALUES(?,?,?)',
                (category, '2026-09-24T00:00:00+00:00', '2026-09-30T12:00:00+00:00'))
    def handler(request):
        query = request.url.params['search_query']
        # This window was already synchronized before this paper appeared in the API.
        ids = [333] if 'cat:math.CO' in query and '202609280000 TO 202609290000' in query else []
        return httpx.Response(200, text=atom(ids))
    install_mock(monkeypatch, handler)
    assert await fetch.fetch_arxiv() == 1
    assert one("SELECT id FROM papers WHERE arxiv_id='2609.00333'")


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [
    atom([], total=2),
    '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/api/errors</id><summary>API error</summary></entry></feed>',
])
async def test_incomplete_or_error_atom_never_advances_cursor(client, monkeypatch, payload):
    install_mock(monkeypatch, lambda request: httpx.Response(200, text=payload))
    with pytest.raises(RuntimeError):
        await fetch.fetch_arxiv()
    assert one("SELECT synced_through FROM arxiv_cursors WHERE category='cs.AI'")['synced_through'] == '2026-09-24T00:00:00+00:00'


@pytest.mark.asyncio
@pytest.mark.parametrize('changes', [
    {},
    {'title': 'Updated paper title'},
    {'abstract': 'Updated research findings.'},
    {'title': 'Updated paper title', 'abstract': 'Updated research findings.'},
], ids=['version-only', 'title-changed', 'abstract-changed', 'both-changed'])
async def test_version_update_regenerates_brief_only_when_its_input_changes(client, monkeypatch, changes):
    from app.config import now
    from app.db import dumps, set_paper_vector
    from app.pipeline import tldr

    paper = {'arxiv_id': '2610.12345', 'arxiv_version': 1,
             'title': 'Original paper title', 'abstract': 'Original research findings.',
             'authors': ['Original Author'], 'primary_category': 'cs.AI', 'categories': ['cs.AI']}
    assert fetch.upsert_paper(paper)
    ident = one('SELECT id FROM papers')['id']
    old_brief = dumps({'title_zh': '旧标题', 'problem': '旧研究问题', 'contribution_result': '旧贡献和结果'})
    execute('UPDATE papers SET brief_json=?,tldr=?,fulltext=?,pdf_path=?,skeleton=?,scored=1,classified=1 WHERE id=?',
            (old_brief, '旧贡献和结果', dumps({'text': 'Old manuscript'}), 'old.pdf', '{}', ident))
    set_paper_vector(ident, [1., 0., 0., 0.])
    execute("INSERT INTO reading_cards(paper_id,status,card_json,created_at) VALUES(?,'ready',?,?)",
            (ident, dumps({'tldr': 'Old full-text reading card'}), now()))

    updated = {**paper, **changes, 'arxiv_version': 2, 'authors': ['Updated Author']}
    assert not fetch.upsert_paper(updated)
    saved = one('SELECT * FROM papers WHERE id=?', (ident,))
    assert saved['arxiv_version'] == 2 and json.loads(saved['authors']) == ['Updated Author']
    assert saved['title'] == updated['title'] and saved['abstract'] == updated['abstract']
    assert saved['brief_json'] == (None if changes else old_brief)
    assert saved['tldr'] == (None if changes else '旧贡献和结果')
    assert all(saved[field] is None for field in ('fulltext', 'pdf_path', 'skeleton', 'embedding'))
    assert saved['scored'] == 0 and saved['classified'] == 0
    assert one('SELECT paper_id FROM papers_vec WHERE paper_id=?', (ident,)) is None
    assert one('SELECT paper_id FROM reading_cards WHERE paper_id=?', (ident,)) is None

    calls = []
    async def complete(feature, messages, **kwargs):
        assert feature == 'brief'
        calls.append(messages[-1]['content'])
        return tldr.PaperBrief(title_zh='新标题', problem='新研究问题', contribution_result='新贡献和结果')
    monkeypatch.setattr(tldr.models, 'complete', complete)
    monkeypatch.setattr(tldr.models, 'concurrency', lambda feature: 1)
    monkeypatch.setattr(tldr.models, 'selected', lambda feature: {'model': 'test-model'})
    assert await tldr.tldr_gen() == int(bool(changes))
    assert calls == ([updated['title'] + '\n' + updated['abstract']] if changes else [])

    cached = one('SELECT brief_json,tldr FROM papers WHERE id=?', (ident,))
    assert not fetch.upsert_paper({**updated, 'arxiv_version': 3})
    assert one('SELECT brief_json,tldr FROM papers WHERE id=?', (ident,)) == cached
    assert await tldr.tldr_gen() == 0
    assert len(calls) == int(bool(changes))
