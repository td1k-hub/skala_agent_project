import json

import pytest

from pump_guard.research_data import import_dataset
from pump_guard.gateway import Gateway
from test_chat import make_app, client_for
from test_research_data import dataset
from test_step2 import respond


def configured_app(tmp_path, dataset, **kwargs):
    source, root = dataset
    import_dataset(source, root, expected_cycles=2)
    app, settings, captured = make_app(tmp_path, **kwargs)
    settings.research_database_path = root / "processed/uci447.sqlite3"
    return app, settings, captured


async def research_session(client, cycle=2):
    response = await client.post('/api/v1/chat/sessions', json={'equipment_id': 'UCI-447', 'research_cycle_id': cycle})
    assert response.status_code == 201, response.text
    return response.json()['id']


async def save_note(client, cycle=2, note='외관 누유 흔적 확인, 추가 계측 필요', outcome='repair_required'):
    response = await client.post('/api/v1/inspections', json={
        'equipment_id': 'UCI-447', 'research_cycle_id': cycle, 'checks': ['leak'], 'outcome': outcome, 'note': note})
    assert response.status_code == 201, response.text


async def test_measured_assessment_reads_real_mcp_records_and_persists_cycle(tmp_path, dataset):
    app, settings, _ = configured_app(tmp_path, dataset)
    async with client_for(app) as client:
        await save_note(client)
        # A different asset's note must never appear in UCI consultation.
        await client.post('/api/v1/inspections', json={'equipment_id': 'P-101', 'checks': ['visual'], 'outcome': 'pass', 'note': 'OTHER_ASSET_PRIVATE_NOTE'})
        session = await research_session(client)
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages', json={'message': '최근 점검 기록과 실측 데이터를 보고 이 장비 상태를 추측해줘'})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['research_cycle_id'] == 2 and result['analysis_status'] is None
        assert {'maintenance_history', 'measured_history'} <= {s['kind'] for s in result['sources']}
        assert '심한 내부 누설' in result['answer'] and '정비 필요' in result['answer']
        assert 'OTHER_ASSET_PRIVATE_NOTE' not in json.dumps(result)
        measured = json.loads(next(s['content'] for s in result['sources'] if s['kind'] == 'measured_history'))
        assert [row['cycle_id'] for row in measured['items']] == [1, 2]
        assert measured['items'][-1]['means']['VS1'] == 29.5
        assert measured['items'][-1]['labels']['pump_leakage'] == 2
        assert result['decision_supported'] is False
        store = app.state.chat.store.get(session)
        assert store['research_cycle_id'] == 2 and len(store['messages']) == 2


async def test_absent_or_later_cycle_records_do_not_establish_health(tmp_path, dataset):
    app, _, _ = configured_app(tmp_path, dataset)
    async with client_for(app) as client:
        await save_note(client, cycle=2, note='FUTURE_CYCLE_NOTE')
        session = await research_session(client, cycle=1)
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages', json={'message': '최근 점검 기록 보고 상태를 추정해줘'})
        assert response.status_code == 200, response.text
        assert '근거가 부족' in response.json()['answer']
        assert 'FUTURE_CYCLE_NOTE' not in json.dumps(response.json())


async def test_explicit_assessment_uses_fixed_evidence_plan_before_ollama_answer(tmp_path, dataset):
    def behavior(prompt):
        if prompt['task'] == 'chat_router':
            pytest.fail('Explicit assessment must not depend on an LLM router')
    app, _, captured = configured_app(tmp_path, dataset, ollama=True, behavior=behavior)
    async with client_for(app) as client:
        await save_note(client)
        session = await research_session(client)
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages/stream', json={'message': '최근 점검 기록을 보고 상태를 추측해줘'})
        assert 'event: result' in response.text, response.text
        assert [p['task'] for p in captured] == ['chat_answer', 'chat_review']
        answer_context = next(p['context'] for p in captured if p['task'] == 'chat_answer')
        assert answer_context['research_cycle_id'] == 2
        assert answer_context['intent'] == 'assessment'
        assert {'maintenance_history', 'measured_history'} <= {s['kind'] for s in answer_context['sources']}
        measured = json.loads(next(s['content'] for s in answer_context['sources'] if s['kind'] == 'measured_history'))
        assert measured['equipment_id'] == 'UCI-447'


async def test_assessment_without_both_citations_is_rejected(tmp_path, dataset):
    def behavior(prompt):
        if prompt['task'] in ('chat_answer', 'chat_repair'):
            source = next(s for s in prompt['context']['sources'] if s['kind'] == 'measured_history')
            return respond({'sections': [{'text': 'UNCHECKED_ASSESSMENT', 'source_ids': [source['id']]}], 'analysis_status': None, 'follow_up_questions': []})
    app, _, _ = configured_app(tmp_path, dataset, ollama=True, behavior=behavior)
    async with client_for(app) as client:
        session = await research_session(client)
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages', json={'message': '최근 점검 기록 보고 상태를 추측해줘'})
        assert response.status_code == 502
        assert app.state.chat.store.get(session)['messages'] == []
        assert 'UNCHECKED_ASSESSMENT' not in response.text


async def test_research_context_cannot_be_assigned_to_other_asset_or_missing_cycle(tmp_path, dataset):
    app, settings, _ = configured_app(tmp_path, dataset)
    async with client_for(app) as client:
        for payload in ({'equipment_id': 'P-101', 'research_cycle_id': 1},
                        {'equipment_id': 'UCI-447'}, {'equipment_id': 'UCI-447', 'research_cycle_id': 3}):
            assert (await client.post('/api/v1/chat/sessions', json=payload)).status_code == 422
        wrong = await client.post('/api/v1/inspections', json={'equipment_id': 'P-101', 'research_cycle_id': 1, 'checks': ['visual'], 'outcome': 'pass'})
        assert wrong.status_code == 422
    gateway = Gateway(settings)
    async with gateway.session('equipment') as session:
        result = await session.call_tool('get_measured_history', {'equipment_id': 'P-101', 'cycle_id': 1})
        assert result.isError


async def test_regular_equipment_assessment_uses_only_its_records(tmp_path, dataset):
    app, _, _ = configured_app(tmp_path, dataset)
    async with client_for(app) as client:
        await client.post('/api/v1/inspections', json={'equipment_id': 'P-101', 'checks': ['vibration'], 'outcome': 'follow_up', 'note': '진동 재측정 필요'})
        session = (await client.post('/api/v1/chat/sessions', json={'equipment_id': 'P-101'})).json()['id']
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages', json={'message': '최근 점검 기록 보고 이 장비 상태를 추측해줘'})
        assert response.status_code == 200, response.text
        result = response.json()
        assert {'analysis_history', 'maintenance_history'} <= {s['kind'] for s in result['sources']}
        assert 'measured_history' not in {s['kind'] for s in result['sources']}
        assert '진동 재측정 필요' in result['answer']


async def test_reported_p101_question_bypasses_router_but_still_reviews_answer(tmp_path):
    def behavior(prompt):
        if prompt['task'] == 'chat_router':
            pytest.fail('The reported explicit assessment must bypass classification')
    app, _, captured = make_app(tmp_path, ollama=True, behavior=behavior)
    async with client_for(app) as client:
        session = (await client.post('/api/v1/chat/sessions', json={'equipment_id': 'P-101'})).json()['id']
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages', json={'message': '최근 점검 기록과 실측 데이터를 보고 이 장비의 상태를 추측해줘. 근거와 추가로 확인할 점검 항목도 알려줘.'})
        assert response.status_code == 200, response.text
        assert [p['task'] for p in captured] == ['chat_answer', 'chat_review']
        assert {'maintenance_history', 'analysis_history'} <= {s['kind'] for s in response.json()['sources']}
        assert 'measured_history' not in {s['kind'] for s in response.json()['sources']}
