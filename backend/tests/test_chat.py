import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from pump_guard.config import Settings
from pump_guard.main import create_app
from pump_guard.llm import OllamaAdapter, OllamaChatModel
from pump_guard.pipeline import Pipeline
from pump_guard.chat_store import ConversationStore, ChatProblem
from test_step2 import EXAMPLE, respond


def make_app(tmp_path, *, ollama=False, behavior=None):
    settings = Settings(_env_file=None, llm_mode='ollama' if ollama else 'mock', database_path=tmp_path / 'chat.sqlite3', analysis_timeout_seconds=10, chat_grounded_fallback=False)
    app = create_app(settings)
    captured = []
    if ollama:
        async def handler(request):
            prompt = json.loads(json.loads(request.content)['messages'][1]['content'])
            captured.append(prompt)
            response = behavior(prompt) if behavior else None
            if asyncio.iscoroutine(response):
                response = await response
            if response is not None:
                return response
            context = prompt['context']
            if prompt['task'] == 'chat_router':
                return respond({'intent': 'equipment', 'tools': ['get_equipment', 'get_maintenance_history'], 'query': None})
            if prompt['task'] in ('chat_answer', 'chat_repair'):
                return respond({'sections': [{'text': '선택한 설비의 사양과 저장된 점검 이력을 조회했습니다.', 'source_ids': [s['id'] for s in context['sources']]}], 'analysis_status': context['canonical_status'], 'follow_up_questions': ['정비 매뉴얼도 확인할까요?']})
            if prompt['task'] == 'chat_review':
                return respond({'verdict': 'approve', 'reason': 'supported'})
            pytest.fail(f"Unexpected model task: {prompt['task']}")
        model = OllamaChatModel(model=settings.ollama_model, base_url=settings.ollama_base_url, transport=httpx.MockTransport(handler))
        app.state.pipeline = Pipeline(settings, llm=OllamaAdapter(settings, model=model))
    return app, settings, captured


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')


async def create_session(client, equipment='P-101', analysis=None):
    response = await client.post('/api/v1/chat/sessions', json={'equipment_id': equipment, 'analysis_run_id': analysis})
    assert response.status_code == 201, response.text
    return response.json()['id']


def stream_events(text):
    events = []
    for block in text.split('\n\n'):
        if not block.startswith('id:'):
            continue
        lines = block.splitlines()
        event = next(line[7:] for line in lines if line.startswith('event: '))
        data = json.loads(next(line[6:] for line in lines if line.startswith('data: ')))
        events.append((event, data))
    return events


async def test_mock_chat_stream_sources_history_and_delete(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with client_for(app) as client:
        session = await create_session(client)
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages/stream', json={'message': '캐비테이션 매뉴얼의 점검 항목을 알려줘'})
        events = stream_events(response.text)
        assert [event for event, _ in events] == ['started'] + ['stage'] * 8 + ['result']
        result = events[-1][1]
        assert result['session_id'] == session and result['mode'] == 'mock'
        assert result['sources'][0]['id'] == 'KB-DEMO-02'
        assert 'NPSH' in result['answer'] and result['decision_supported'] is False
        stored = (await client.get(f'/api/v1/chat/sessions/{session}')).json()
        assert [m['role'] for m in stored['messages']] == ['user', 'assistant']
        assert stored['messages'][1]['response']['message_id'] == result['message_id']
        assert stored['busy'] is False
        overview = (await client.get('/api/v1/admin/runs')).json()['items'][0]
        assert overview['kind'] == 'chat' and overview['state'] == 'completed'
        assert '캐비테이션' not in json.dumps(overview, ensure_ascii=False)
        assert (await client.delete(f'/api/v1/chat/sessions/{session}')).status_code == 204
        assert (await client.get(f'/api/v1/chat/sessions/{session}')).status_code == 404


async def test_analysis_snapshot_context_is_server_owned_and_equipment_checked(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with client_for(app) as client:
        result = (await client.post('/api/v1/analyses', json=EXAMPLE)).json()
        session = await create_session(client, analysis=result['run_id'])
        stored = (await client.get(f'/api/v1/chat/sessions/{session}')).json()
        assert stored['analysis']['result']['status'] == '주의'
        assert stored['analysis']['measurements'] == EXAMPLE['measurements']
        reply = await client.post(f'/api/v1/chat/sessions/{session}/messages', json={'message': '왜 이런 분석 결과가 나왔어?'})
        assert reply.status_code == 200, reply.text
        assert reply.json()['analysis_status'] == '주의'
        assert reply.json()['sources'][0]['id'] == f"analysis:{result['run_id']}"
        mismatch = await client.post('/api/v1/chat/sessions', json={'equipment_id': 'P-102', 'analysis_run_id': result['run_id']})
        assert mismatch.status_code == 409
        absent = await client.post('/api/v1/chat/sessions', json={'equipment_id': 'P-101', 'analysis_run_id': str(uuid4())})
        assert absent.status_code == 404


async def test_normal_range_question_answers_range_even_with_linked_analysis(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with client_for(app) as client:
        analysis = (await client.post('/api/v1/analyses', json=EXAMPLE)).json()
        session = await create_session(client, analysis=analysis['run_id'])
        # The same question previously went to the saved analysis summary whenever
        # it contained "pressure", regardless of what the user actually asked.
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages', json={'message': '유량이나 토출압력 정상수치가 어느정도야?'})
        assert response.status_code == 200, response.text
        answer = response.json()['answer']
        assert '정격 유량은 100' in answer
        assert '정상 운전 범위를 뜻하지 않습니다' in answer
        assert '토출 압력의 정상 범위' not in answer or '정상 범위는 등록되어 있지 않습니다' in answer
        assert '압력의 정상 범위는 등록되어 있지 않습니다' in answer
        assert all(source['kind'] == 'equipment' for source in response.json()['sources'])


async def test_linked_analysis_chat_builds_maintenance_plan_from_saved_sources(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with client_for(app) as client:
        earlier = {**EXAMPLE, 'measurements': {**EXAMPLE['measurements'], 'vibration_mm_s': 2.5}}
        first = await client.post('/api/v1/analyses', json=earlier)
        assert first.status_code == 200, first.text
        analysis = (await client.post('/api/v1/analyses', json=EXAMPLE)).json()
        session = await create_session(client, analysis=analysis['run_id'])
        check = await client.post('/api/v1/inspections', json={'equipment_id': 'P-101', 'checks': ['vibration'], 'outcome': 'repair_required', 'note': '진동 재확인 필요'})
        assert check.status_code == 201, check.text
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages', json={'message': '이 분석 결과와 이전 실측 기록을 바탕으로 정비 계획을 세워줘. 남은 수명은?'})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['analysis_status'] == analysis['status']
        assert {'analysis', 'maintenance_history', 'analysis_history'} == {s['kind'] for s in data['sources']}
        assert '정비 필요' in data['answer'] and '2.5→4.2' in data['answer']
        assert '잔여 수명이나 교체일은 계산할 수 없습니다' in data['answer']
        assert analysis['maintenance_plan'][0]['instruction'] in data['answer']


async def test_maintenance_plan_question_without_linked_analysis_requests_analysis(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with client_for(app) as client:
        session = await create_session(client)
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages', json={'message': '정비 계획을 세워줘'})
        assert response.status_code == 200, response.text
        assert '분석 결과가 연결되지 않았습니다' in response.json()['answer']


async def test_saved_analyses_drive_trend_consultation_without_synthetic_history(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with client_for(app) as client:
        first_input = {**EXAMPLE, 'measurements': {**EXAMPLE['measurements'], 'vibration_mm_s': 2.0}}
        first = await client.post('/api/v1/analyses', json=first_input)
        second = await client.post('/api/v1/analyses', json=EXAMPLE)
        assert first.status_code == second.status_code == 200
        history = await client.get('/api/v1/equipment/P-101/analysis-history')
        assert history.status_code == 200
        rows = history.json()['items']
        assert [row['run_id'] for row in rows[:2]] == [second.json()['run_id'], first.json()['run_id']]
        assert rows[0]['measurements']['vibration_mm_s'] == 4.2
        assert (await client.get('/api/v1/equipment/P-102/analysis-history')).json()['items'] == []
        assert [row['run_id'] for row in ConversationStore(app.state.chat.settings.database_path).list_analyses('P-101', through_run_id=first.json()['run_id'])] == [first.json()['run_id']]
        session = await create_session(client, analysis=second.json()['run_id'])
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages', json={'message': '진동 수치가 이전보다 얼마나 증가했어? 정상 흐름이야?'})
        assert response.status_code == 200, response.text
        answer = response.json()['answer']
        assert '2→4.2 mm/s (+2.2)' in answer
        assert '정상 운전 여부' in answer
        assert 'operating_history' not in {source['kind'] for source in response.json()['sources']}


async def test_current_analysis_status_question_uses_linked_result(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with client_for(app) as client:
        analysis = (await client.post('/api/v1/analyses', json=EXAMPLE)).json()
        session = await create_session(client, analysis=analysis['run_id'])
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages', json={'message': '이번 분석 상태 어때?'})
        assert response.status_code == 200, response.text
        assert analysis['status'] in response.json()['answer']
        assert {source['kind'] for source in response.json()['sources']} == {'analysis'}


async def test_unlinked_analysis_question_requests_context_instead_of_inventing(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with client_for(app) as client:
        session = await create_session(client)
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages', json={'message': '이번 분석이 왜 주의야?'})
        assert response.status_code == 200
        result = response.json()
        assert '연결되지 않았습니다' in result['answer']
        assert result['sources'] == [] and result['analysis_status'] is None


async def test_real_ollama_adapter_routes_retrieves_and_reviews_with_bounded_history(tmp_path):
    app, settings, captured = make_app(tmp_path, ollama=True)
    async with client_for(app) as client:
        session = await create_session(client)
        for text in ['최근 점검 이력이 있어?', '그 설비의 사양도 알려줘']:
            response = await client.post(f'/api/v1/chat/sessions/{session}/messages', json={'message': text})
            assert response.status_code == 200, response.text
            result = response.json()
            assert result['mode'] == 'ollama'
            assert len(result['metrics']['llm_calls']) == 3
        assert [p['task'] for p in captured] == ['chat_router', 'chat_answer', 'chat_review'] * 2
        assert len(captured[3]['context']['recent_conversation']) == 2
        assert captured[3]['context']['recent_conversation'][0]['role'] == 'user'
    async with client_for(create_app(settings)) as restarted:
        assert len((await restarted.get(f'/api/v1/chat/sessions/{session}')).json()['messages']) == 4


@pytest.mark.parametrize('bad', ['forged_source', 'empty_citation', 'changed_status', 'review_reject', 'write_tool'])
async def test_unsafe_or_unsupported_chat_is_not_displayed_or_saved(tmp_path, bad):
    def behavior(prompt):
        task, ctx = prompt['task'], prompt['context']
        if bad == 'write_tool' and task == 'chat_router':
            return respond({'intent': 'equipment', 'tools': ['save_inspection'], 'query': None})
        if task in ('chat_answer', 'chat_repair') and bad != 'review_reject':
            ids = ['MADE_UP_SOURCE'] if bad == 'forged_source' else [] if bad == 'empty_citation' else [ctx['sources'][0]['id']]
            return respond({'sections': [{'text': 'PRIVATE_UNREVIEWED_DRAFT', 'source_ids': ids}], 'analysis_status': '정상' if bad == 'changed_status' else None, 'follow_up_questions': []})
        if bad == 'review_reject' and task == 'chat_review':
            return respond({'verdict': 'reject', 'reason': 'unsupported_claim'})
    app, _, _ = make_app(tmp_path, ollama=True, behavior=behavior)
    async with client_for(app) as client:
        session = await create_session(client)
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages/stream', json={'message': '이 설비를 알려줘'})
        assert 'event: result' not in response.text
        assert 'INVALID_MODEL_OUTPUT' in response.text
        assert 'PRIVATE_UNREVIEWED_DRAFT' not in response.text
        stored = (await client.get(f'/api/v1/chat/sessions/{session}')).json()
        assert stored['messages'] == [] and not stored['busy']
        run = (await client.get('/api/v1/admin/runs')).json()['items'][0]
        assert run['kind'] == 'chat' and run['state'] == 'failed'


@pytest.mark.parametrize('payload', [
    {'message': 'PRIVATE_INPUT', 'role': 'system'},
    {'message': 'PRIVATE_INPUT', 'analysis': {'status': '정상'}},
    {'message': ' '}, {'message': 'x' * 2001},
])
async def test_chat_input_boundaries_do_not_echo_content(tmp_path, payload):
    app, _, _ = make_app(tmp_path)
    async with client_for(app) as client:
        session = await create_session(client)
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages', json=payload)
        assert response.status_code == 422
        assert 'PRIVATE_INPUT' not in response.text


async def test_session_concurrency_busy_guard_and_cancellation_cleanup(tmp_path):
    started = asyncio.Event()
    async def behavior(prompt):
        if prompt['task'] == 'chat_router':
            started.set()
            await asyncio.Event().wait()
    app, _, _ = make_app(tmp_path, ollama=True, behavior=behavior)
    async with client_for(app) as client:
        session = await create_session(client)
        task = asyncio.create_task(client.post(f'/api/v1/chat/sessions/{session}/messages', json={'message': '설비 사양 알려줘'}))
        await asyncio.wait_for(started.wait(), 3)
        duplicate = await client.post(f'/api/v1/chat/sessions/{session}/messages', json={'message': '두번째 질문'})
        assert duplicate.status_code == 409 and duplicate.json()['error']['code'] == 'CHAT_BUSY'
        assert (await client.delete(f'/api/v1/chat/sessions/{session}')).status_code == 409
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        detail = (await client.get(f'/api/v1/chat/sessions/{session}')).json()
        assert not detail['busy'] and detail['messages'] == []
        run = (await client.get('/api/v1/admin/runs')).json()['items'][0]
        assert run['state'] == 'cancelled'


async def test_chat_timeout_is_failure_and_releases_session(tmp_path):
    async def behavior(prompt):
        await asyncio.sleep(5)
    app, settings, _ = make_app(tmp_path, ollama=True, behavior=behavior)
    settings.analysis_timeout_seconds = .04
    settings.heartbeat_seconds = .01
    async with client_for(app) as client:
        session = await create_session(client)
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages/stream', json={'message': '사양 알려줘'})
        assert ': heartbeat' in response.text
        assert 'UPSTREAM_TIMEOUT' in response.text and 'event: result' not in response.text
        assert (await client.get('/api/v1/admin/runs')).json()['items'][0]['state'] == 'failed'
        assert not (await client.get(f'/api/v1/chat/sessions/{session}')).json()['busy']


async def test_sessions_do_not_share_history_or_context(tmp_path):
    app, _, captured = make_app(tmp_path, ollama=True)
    async with client_for(app) as client:
        first = await create_session(client, 'P-101')
        second = await create_session(client, 'P-102')
        assert (await client.post(f'/api/v1/chat/sessions/{first}/messages', json={'message': 'ONLY_FIRST_CONVERSATION'})).status_code == 200
        assert (await client.post(f'/api/v1/chat/sessions/{second}/messages', json={'message': 'SECOND_CONVERSATION'})).status_code == 200
        second_prompt = captured[3]['context']
        assert second_prompt['equipment_id'] == 'P-102' and second_prompt['recent_conversation'] == []
        assert 'ONLY_FIRST_CONVERSATION' not in json.dumps(second_prompt)
        assert len((await client.get('/api/v1/chat/sessions?equipment_id=P-101')).json()['items']) == 1


def test_database_lease_is_shared_by_independent_store_instances(tmp_path):
    first = ConversationStore(tmp_path / 'lease.sqlite3')
    second = ConversationStore(tmp_path / 'lease.sqlite3')
    session = first.create('P-101')['id']
    first.claim(session, 'first', 100)
    with pytest.raises(ChatProblem, match='CHAT_BUSY'):
        second.claim(session, 'second', 100)
    second.release(session, 'wrong-run')
    assert first.get(session)['busy']
    first.release(session, 'first')
    second.claim(session, 'second', 100)


async def test_chat_disconnect_cancels_worker_and_releases_session(tmp_path):
    started = asyncio.Event()
    async def behavior(prompt):
        started.set()
        await asyncio.Event().wait()
    app, _, _ = make_app(tmp_path, ollama=True, behavior=behavior)
    async with client_for(app) as client:
        session = await create_session(client)
    body = json.dumps({'message': '설비 사양을 알려줘'}).encode()
    first = True
    async def receive():
        nonlocal first
        if first:
            first = False
            return {'type': 'http.request', 'body': body}
        await started.wait()
        return {'type': 'http.disconnect'}
    async def send(message):
        pass
    scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.0'}, 'http_version': '1.1', 'method': 'POST', 'scheme': 'http',
             'path': f'/api/v1/chat/sessions/{session}/messages/stream', 'raw_path': b'/', 'query_string': b'',
             'headers': [(b'content-type', b'application/json')], 'client': ('127.0.0.1', 1), 'server': ('test', 80)}
    await asyncio.wait_for(app(scope, receive, send), 3)
    assert not app.state.chat.store.get(session)['busy']
    assert app.state.chat.store.get(session)['messages'] == []
    assert app.state.pipeline.store.list()[0]['state'] == 'cancelled'


def test_monitoring_migrates_existing_analysis_database(tmp_path):
    import sqlite3
    from pump_guard.monitoring import RunStore
    database = tmp_path / 'old.sqlite3'
    with sqlite3.connect(database) as con:
        con.execute("""CREATE TABLE analysis_runs (run_id TEXT PRIMARY KEY, equipment_id TEXT NOT NULL,
          mode TEXT NOT NULL, model TEXT, state TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
          error_code TEXT, metrics TEXT NOT NULL DEFAULT '{}', trace_id TEXT, tracing_enabled INTEGER NOT NULL DEFAULT 0)""")
        con.execute("INSERT INTO analysis_runs (run_id,equipment_id,mode,state,started_at) VALUES ('old','P-101','mock','completed','2026-10-21')")
    store = RunStore(database)
    assert store.list()[0]['kind'] == 'analysis'
    settings = Settings(_env_file=None, database_path=database)
    store.begin('new', 'P-101', settings, 'trace', kind='chat')
    assert store.detail('new')['kind'] == 'chat'


async def test_rejected_draft_is_revised_once_and_independently_reviewed(tmp_path):
    reviews = 0
    def behavior(prompt):
        nonlocal reviews
        if prompt['task'] == 'chat_answer':
            return respond({'sections': [{'text': 'FIRST_REJECTED_DRAFT', 'source_ids': [s['id'] for s in prompt['context']['sources']]}], 'analysis_status': None, 'follow_up_questions': []})
        if prompt['task'] == 'chat_review':
            reviews += 1
            return respond({'verdict': 'reject', 'reason': 'unsupported_claim'} if reviews == 1 else {'verdict': 'approve', 'reason': 'supported'})
    app, _, captured = make_app(tmp_path, ollama=True, behavior=behavior)
    async with client_for(app) as client:
        session = await create_session(client)
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages/stream', json={'message': '설비 사양을 알려줘'})
        events = stream_events(response.text)
        result = next(data for event, data in events if event == 'result')
        assert 'FIRST_REJECTED_DRAFT' not in response.text
        assert [p['task'] for p in captured] == ['chat_router', 'chat_answer', 'chat_review', 'chat_repair', 'chat_review']
        repair = next(p['context'] for p in captured if p['task'] == 'chat_repair')
        assert repair['review_feedback']['reason'] == 'unsupported_claim'
        stored = app.state.chat.store.get(session)
        assert stored['messages'][1]['content'] == result['answer']
        detail = app.state.pipeline.store.detail(result['run_id'])
        assert any(a['decision'] == 'rejected_unsupported_claim' for a in detail['agents'])
        assert 'FIRST_REJECTED_DRAFT' not in json.dumps(detail)


@pytest.mark.parametrize('failure', ['second_rejection', 'forged_repair'])
async def test_failed_repair_never_bypasses_citations_or_review(tmp_path, failure):
    def behavior(prompt):
        if prompt['task'] == 'chat_review':
            return respond({'verdict': 'reject', 'reason': 'unsupported_claim'})
        if prompt['task'] == 'chat_repair' and failure == 'forged_repair':
            return respond({'sections': [{'text': 'UNAPPROVED_REPAIR', 'source_ids': ['FORGED_ID']}], 'analysis_status': None, 'follow_up_questions': []})
    app, _, captured = make_app(tmp_path, ollama=True, behavior=behavior)
    async with client_for(app) as client:
        session = await create_session(client)
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages', json={'message': '설비 사양을 알려줘'})
        assert response.status_code == 502
        assert 'UNAPPROVED_REPAIR' not in response.text
        assert app.state.chat.store.get(session)['messages'] == []
        assert sum(p['task'] == 'chat_repair' for p in captured) == 1
        if failure == 'second_rejection':
            assert '출처로 확인되지 않은 내용' in response.json()['error']['message']


async def test_invalid_draft_citation_is_repaired_before_independent_review(tmp_path):
    def behavior(prompt):
        if prompt['task'] == 'chat_answer':
            return respond({'sections': [{'text': 'INVALID_FIRST_DRAFT', 'source_ids': ['WRONG_ID']}], 'analysis_status': None, 'follow_up_questions': []})
    app, _, captured = make_app(tmp_path, ollama=True, behavior=behavior)
    async with client_for(app) as client:
        session = await create_session(client)
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages', json={'message': '추가 점검 항목을 알려줘'})
        assert response.status_code == 200, response.text
        assert 'INVALID_FIRST_DRAFT' not in response.text
        assert [p['task'] for p in captured] == ['chat_router', 'chat_answer', 'chat_repair', 'chat_review']
        repair = next(p['context'] for p in captured if p['task'] == 'chat_repair')
        assert repair['validation_feedback'] == 'invalid_citation'
        assert 'WRONG_ID' not in repair['allowed_source_ids']
        detail = app.state.pipeline.store.detail(response.json()['run_id'])
        assert any(event['decision'] == 'invalid_citation' for event in detail['agents'])


async def test_inline_source_metadata_is_removed_without_losing_structured_citations(tmp_path):
    def behavior(prompt):
        if prompt['task'] == 'chat_answer':
            ids = [s['id'] for s in prompt['context']['sources']]
            return respond({'sections': [{'text': f"자료를 확인했습니다. (source_ids: {ids!r})", 'source_ids': ids}], 'analysis_status': None, 'follow_up_questions': []})
    app, _, _ = make_app(tmp_path, ollama=True, behavior=behavior)
    async with client_for(app) as client:
        session = await create_session(client)
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages', json={'message': '설비 사양을 알려줘'})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['answer'] == '자료를 확인했습니다.'
        assert result['sections'][0]['source_ids']
        assert app.state.chat.store.get(session)['messages'][1]['content'] == result['answer']
