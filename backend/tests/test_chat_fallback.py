import json

import pytest

from pump_guard.models import AnalysisResult, Measurements
from test_chat import make_app, client_for, create_session
from test_step2 import EXAMPLE, respond


@pytest.mark.parametrize('failure', ['schema', 'citation', 'review'])
async def test_invalid_llm_answer_returns_only_server_grounded_summary(tmp_path, failure):
    def behavior(prompt):
        task = prompt['task']
        if task in ('chat_answer', 'chat_repair'):
            if failure == 'schema':
                return respond({})
            if failure == 'citation':
                return respond({'sections': [{'text': 'UNSUPPORTED_MODEL_TEXT', 'source_ids': ['MADE_UP']}], 'analysis_status': None, 'follow_up_questions': []})
        if failure == 'review' and task == 'chat_review':
            return respond({'verdict': 'reject', 'reason': 'unsupported_claim'})
    app, settings, _ = make_app(tmp_path, ollama=True, behavior=behavior)
    settings.chat_grounded_fallback = True
    async with client_for(app) as client:
        session = await create_session(client)
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages', json={'message': '최근 점검 기록을 보고 상태를 추측해줘'})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['response_kind'] == 'source_summary'
        assert 'UNSUPPORTED_MODEL_TEXT' not in result['answer']
        assert '근거가 부족' in result['answer']
        assert result['analysis_status'] is None
        assert {'maintenance_history', 'analysis_history'} <= {s['kind'] for s in result['sources']}
        ids = {s['id'] for s in result['sources']}
        assert all(set(s['source_ids']) <= ids for s in result['sections'])
        assert len(app.state.chat.store.get(session)['messages']) == 2


async def test_pressure_question_explains_unassessed_scope_without_model_call(tmp_path):
    def behavior(prompt):
        pytest.fail('Stored pressure scope must be explained directly from its snapshot')
    app, _, _ = make_app(tmp_path, ollama=True, behavior=behavior)
    measurements = Measurements.model_validate({**EXAMPLE['measurements'], 'vibration_mm_s': 1.5, 'bearing_temperature_c': 62.0, 'suction_pressure_bar': -0.5})
    snapshot = AnalysisResult(run_id='f584eb27-b6c0-4813-b0dc-48e5bfa6d0eb', equipment_id='P-101', status='정상', missing_fields=[], findings=[], candidate_causes=[], checklist=[], safety_notice='')
    app.state.chat.store.save_analysis(snapshot, measurements)
    async with client_for(app) as client:
        session = await create_session(client, analysis=snapshot.run_id)
        response = await client.post(f'/api/v1/chat/sessions/{session}/messages', json={'message': '흡입 압력이 비정상인것같은데 왜 정상판정을 했지?'})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result['response_kind'] == 'source_summary'
        assert '흡입 압력 -0.5 bar' in result['answer']
        assert '압력이 정상이라는 뜻이 아닙니다' in result['answer']
        assert result['analysis_status'] == '정상'
        assert 'source_ids:' not in result['answer']
