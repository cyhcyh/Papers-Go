import asyncio
import json

import httpx
import pytest
from openai import AsyncOpenAI

from app import prompts
from app.db import dumps, one, rows
from app.llm import runtime
from app.llm.provider import cloud
from app.pipeline import embed, paper_retries, tldr
from app.pipeline.classify import classify_paper
from app.pipeline.topic_decision import ClassificationError, predict_topic
from app.pipeline_control import cancellation_scope
from .test_brief_lengths import BRIEF, FORMULA, save_old
from .test_research_areas import area, decision, paper


QUALITY = {'summary':'证明给定条件下 '+FORMULA, 'contribution':80, 'evidence_insufficient':False}
BAD = {
    'brief':r'{"title_zh":"译名","problem":"研究给定条件下的上界","contribution_result":"原始失败输出 $\gamma$"}',
    'quality':r'{"summary":"原始失败输出 $\gamma$","contribution":80,"evidence_insufficient":false}',
}
GENERATE = {'brief':tldr.generate_brief, 'quality':embed.score_paper}
TASK = {'brief':'tldr_gen', 'quality':'assess_quality'}


def configuration(kind='cloud', fallback=False):
    config = runtime.legacy_defaults()
    config['connections'][1].update(kind=kind, api_key='test-key')
    for feature in ('brief', 'quality', 'classify', 'chat'):
        config['routes'][feature] = {
            'primary':{'connection_id':'cloud', 'model':'primary', 'thinking':'off'},
            'fallback':{'connection_id':'cloud', 'model':'backup'} if fallback else None,
        }
    return config


def snapshot(ident):
    return one('SELECT brief_json,tldr,skeleton,scored,quality_score FROM papers WHERE id=?', (ident,))


def provider(monkeypatch, kind, request):
    if kind=='ollama':
        from app.llm.ollama import ollama
        monkeypatch.setattr(ollama, 'chat', request)
    elif kind=='codex':
        from app.llm.codex import codex
        monkeypatch.setattr(codex, 'complete', request)
    else:
        monkeypatch.setattr(cloud, 'complete', request)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['cloud', 'ollama', 'codex'])
@pytest.mark.parametrize('feature', ['brief', 'quality'])
async def test_parse_errors_repair_once_for_all_providers_with_unchanged_source(client, papers, monkeypatch, kind, feature):
    calls = []
    valid = BRIEF if feature=='brief' else QUALITY
    original = prompts.get('brief' if feature=='brief' else 'quality')
    async def request(messages, **kwargs):
        calls.append(messages)
        return json.loads(BAD[feature]) if len(calls)==1 else dict(valid)
    provider(monkeypatch, kind, request)
    p = one('SELECT * FROM papers WHERE id=?', (papers[0],))
    with runtime.model_snapshot(configuration(kind), replace=True):
        await GENERATE[feature](p)
    assert len(calls)==2
    if kind=='ollama':
        assert calls[1].startswith(calls[0])
        correction = calls[1]
    else:
        assert calls[1][:2]==calls[0]
        assert calls[1][2]=={'role':'assistant', 'content':BAD[feature]}
        correction = calls[1][3]['content']
    assert original in (calls[0] if kind=='ollama' else calls[0][0]['content'])
    assert BAD[feature] in (calls[1] if kind=='ollama' else calls[1][2]['content'])
    assert 'Invalid' in correction and '字符位置' in correction and 'maxLength' in correction
    if feature=='brief':
        assert json.loads(snapshot(p['id'])['brief_json'])==BRIEF
        assert '1200' in correction and FORMULA in snapshot(p['id'])['tldr']
    else:
        saved = json.loads(snapshot(p['id'])['skeleton'])
        assert saved['summary']==QUALITY['summary'] and saved['contribution']==80
        assert saved['evidence_insufficient'] is True  # Abstract-only safeguard still applies.
    assert not rows('SELECT * FROM paper_task_failures')
    assert all('原始失败输出' not in log['detail'] for log in rows('SELECT detail FROM app_logs'))


@pytest.mark.asyncio
@pytest.mark.parametrize('malformed', [BAD['brief'], dumps(BRIEF)+' extra',
    '{"title_zh":"译名" "problem":"具体问题","contribution_result":"具体贡献"}', ''])
async def test_cloud_sdk_repairs_real_decode_error_and_keeps_api_contract(client, papers, monkeypatch, malformed):
    calls = []
    def respond(request):
        body = json.loads(request.content)
        calls.append(body)
        content = malformed if len(calls)==1 else dumps(BRIEF)
        return httpx.Response(200, json={'id':'test', 'object':'chat.completion', 'created':0,
            'model':'primary', 'choices':[{'index':0, 'finish_reason':'stop',
                'message':{'role':'assistant', 'content':content}}],
            'usage':{'prompt_tokens':5, 'completion_tokens':5, 'total_tokens':10}})
    monkeypatch.setattr(cloud, 'client', lambda:AsyncOpenAI(base_url='https://example.test/v1', api_key='test-key',
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))))
    with runtime.model_snapshot(configuration(), replace=True):
        assert await tldr.generate_brief(one('SELECT * FROM papers WHERE id=?', (papers[0],)))==BRIEF
    assert len(calls)==2
    for body in calls:
        assert body['response_format']=={'type':'json_object'} and body['max_tokens']==2048
    assert calls[1]['messages'][:2]==calls[0]['messages'][:2]
    if malformed:assert calls[1]['messages'][2]['content']==malformed
    assert all(m.get('content') for m in calls[1]['messages'])
    assert len(rows("SELECT * FROM llm_usage WHERE feature='brief'"))==2


@pytest.mark.asyncio
async def test_quality_normal_success_needs_only_one_request(client, papers, monkeypatch):
    calls = []
    async def request(messages, **kwargs):
        calls.append(messages)
        return dict(QUALITY)
    monkeypatch.setattr(cloud, 'complete', request)
    with runtime.model_snapshot(configuration(), replace=True):
        await embed.score_paper(one('SELECT * FROM papers WHERE id=?', (papers[0],)))
    assert len(calls)==1 and len(calls[0])==2


@pytest.mark.asyncio
async def test_one_correction_can_fix_json_and_overlength_together(client, papers, monkeypatch):
    long_result = {**BRIEF, 'contribution_result':'长'*1201+' '+FORMULA}
    malformed = dumps(long_result).replace(r'\\leq', r'\leq')
    calls = []
    async def request(messages, **kwargs):
        calls.append(messages)
        return json.loads(malformed) if len(calls)==1 else dict(BRIEF)
    monkeypatch.setattr(cloud, 'complete', request)
    with runtime.model_snapshot(configuration(), replace=True):
        assert await tldr.generate_brief(one('SELECT * FROM papers WHERE id=?', (papers[0],)))==BRIEF
    assert len(calls)==2 and calls[1][2]['content']==malformed
    assert '1200' in calls[1][3]['content']
    assert FORMULA in snapshot(papers[0])['tldr']


@pytest.mark.asyncio
@pytest.mark.parametrize('second_error', ['length', 'json', 'placeholder'])
async def test_brief_format_and_length_share_one_repair_and_never_publish_invalid_result(client, papers, monkeypatch, second_error):
    before = save_old(papers[0])
    calls = []
    async def request(messages, **kwargs):
        calls.append(messages)
        if second_error=='json':
            if len(calls)==1:return {**BRIEF, 'contribution_result':'长'*1201}
            return json.loads(BAD['brief'])
        if len(calls)==1:return json.loads(BAD['brief'])
        return {**BRIEF, **({'contribution_result':'长'*1201} if second_error=='length' else {'problem':'研究问题'})}
    monkeypatch.setattr(cloud, 'complete', request)
    with runtime.model_snapshot(configuration(), replace=True), pytest.raises(ValueError):
        await tldr.generate_brief(one('SELECT * FROM papers WHERE id=?', (papers[0],)))
    assert len(calls)==2
    assert one('SELECT brief_json,tldr FROM papers WHERE id=?', (papers[0],))==before
    assert one("SELECT failures FROM paper_task_failures WHERE task='tldr_gen'")['failures']==1
    assert '1200' in calls[1][3]['content']


@pytest.mark.asyncio
@pytest.mark.parametrize('feature', ['brief', 'quality'])
async def test_three_failed_rounds_pause_after_six_calls_and_preserve_results(client, papers, monkeypatch, feature):
    before = snapshot(papers[0])
    calls = []
    async def request(*args, **kwargs):
        calls.append(1)
        return json.loads(BAD[feature])
    monkeypatch.setattr(cloud, 'complete', request)
    p = one('SELECT * FROM papers WHERE id=?', (papers[0],))
    with runtime.model_snapshot(configuration(), replace=True):
        for _ in range(3):
            with pytest.raises(json.JSONDecodeError):await GENERATE[feature](p)
        with pytest.raises(paper_retries.PausedError):await GENERATE[feature](p)
    assert len(calls)==6 and snapshot(p['id'])==before
    assert paper_retries.snapshot()[TASK[feature]]=={'failed':1, 'paused':1, 'limit':3}


@pytest.mark.asyncio
@pytest.mark.parametrize('feature', ['brief', 'quality'])
@pytest.mark.parametrize('stop_at', [1, 2])
async def test_stop_during_format_repair_preserves_results_and_failure_budget(client, papers, monkeypatch, feature, stop_at):
    stopped = asyncio.Event()
    before = snapshot(papers[0])
    calls = []
    async def request(*args, **kwargs):
        calls.append(1)
        if len(calls)==stop_at:stopped.set()
        return json.loads(BAD[feature]) if len(calls)==1 else dict(BRIEF if feature=='brief' else QUALITY)
    monkeypatch.setattr(cloud, 'complete', request)
    with cancellation_scope(stopped), runtime.model_snapshot(configuration(), replace=True), pytest.raises(asyncio.CancelledError):
        await GENERATE[feature](one('SELECT * FROM papers WHERE id=?', (papers[0],)))
    assert len(calls)==stop_at and snapshot(papers[0])==before
    assert not rows('SELECT * FROM paper_task_failures')


@pytest.mark.asyncio
@pytest.mark.parametrize('feature', ['brief', 'quality'])
async def test_fallback_shares_format_repair_allowance(client, papers, monkeypatch, feature):
    calls = []
    before = snapshot(papers[0])
    async def request(*args, **kwargs):
        calls.append(runtime.current_binding()['model'])
        return json.loads(BAD[feature])
    monkeypatch.setattr(cloud, 'complete', request)
    with runtime.model_snapshot(configuration(fallback=True), replace=True), pytest.raises(json.JSONDecodeError):
        await GENERATE[feature](one('SELECT * FROM papers WHERE id=?', (papers[0],)))
    assert calls==['primary', 'primary', 'backup']
    assert snapshot(papers[0])==before


@pytest.mark.asyncio
@pytest.mark.parametrize('repaired', [False, True])
async def test_quality_invalid_score_is_rejected_without_clamping_or_extra_repair(client, papers, monkeypatch, repaired):
    before = snapshot(papers[0])
    calls = []
    async def request(*args, **kwargs):
        calls.append(1)
        if repaired and len(calls)==1:return json.loads(BAD['quality'])
        return {**QUALITY, 'contribution':999}
    monkeypatch.setattr(cloud, 'complete', request)
    with runtime.model_snapshot(configuration(), replace=True), pytest.raises(ValueError):
        await embed.score_paper(one('SELECT * FROM papers WHERE id=?', (papers[0],)))
    assert len(calls)==(2 if repaired else 1) and snapshot(papers[0])==before


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_type', ['missing', 'false', 0])
async def test_classification_reports_missing_or_invalid_boolean_without_guessing(client, monkeypatch, bad_type):
    chosen = area('Reinforcement Learning')
    invalid = decision(chosen['key'])
    if bad_type=='missing':invalid.pop('no_suitable_topic')
    else:invalid['no_suitable_topic']=bad_type
    calls = []
    async def request(messages, **kwargs):
        calls.append(messages)
        return dict(invalid) if len(calls)==1 else decision(chosen['key'])
    monkeypatch.setattr(cloud, 'complete', request)
    with runtime.model_snapshot(configuration(), replace=True):
        value = await predict_topic(paper(), [chosen])
    assert len(calls)==2 and value['standard_key']==chosen['key']
    assert value['no_suitable_topic'] is False
    assert json.loads(calls[1][1]['content'])==invalid
    correction = calls[1][2]['content']
    assert 'no_suitable_topic' in correction
    assert ('必填字段缺失' if bad_type=='missing' else '必须是 JSON 布尔值 true 或 false') in correction


@pytest.mark.asyncio
async def test_classification_repair_failure_preserves_previous_topic(client, papers, monkeypatch):
    p = one('SELECT * FROM papers WHERE id=?', (papers[0],))
    before = rows('SELECT * FROM paper_topics WHERE paper_id=?', (p['id'],))
    calls = []
    async def request(*args, **kwargs):
        calls.append(1)
        return decision('RA-CS-001', no_suitable_topic='false')
    monkeypatch.setattr(cloud, 'complete', request)
    with runtime.model_snapshot(configuration(), replace=True), pytest.raises(ClassificationError, match='布尔值'):
        await classify_paper(p)
    assert len(calls)==2 and rows('SELECT * FROM paper_topics WHERE paper_id=?', (p['id'],))==before


@pytest.mark.asyncio
async def test_shortlist_and_full_catalog_share_one_correction(client, monkeypatch):
    chosen = area('Reinforcement Learning')
    outputs = [decision(chosen['key'], no_suitable_topic='false'),
               decision(None, confidence=.1, no_suitable_topic=True),
               {**decision(chosen['key'], no_suitable_topic='false'), 'new_topic':None}]
    calls = []
    async def request(*args, **kwargs):
        calls.append(1)
        return outputs[len(calls)-1]
    monkeypatch.setattr(cloud, 'complete', request)
    with runtime.model_snapshot(configuration(), replace=True), pytest.raises(ClassificationError) as error:
        await predict_topic(paper(), [chosen])
    assert len(calls)==3 and error.value.attempts==3


@pytest.mark.asyncio
async def test_unrelated_features_do_not_gain_json_repair(client, monkeypatch):
    calls = []
    async def request(*args, **kwargs):
        calls.append(1)
        return json.loads(BAD['brief'])
    monkeypatch.setattr(cloud, 'complete', request)
    with runtime.model_snapshot(configuration(), replace=True), pytest.raises(json.JSONDecodeError):
        await runtime.complete('chat', [{'role':'user', 'content':'original'}], json_mode=True)
    assert len(calls)==1
