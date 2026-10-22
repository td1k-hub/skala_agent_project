import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from pump_guard.config import Settings
from pump_guard.llm import OllamaAdapter, OllamaChatModel
from pump_guard.main import create_app
from pump_guard.pipeline import Pipeline
from test_step2 import CAUSE, PLAN, EXAMPLE, default_agent_turn, respond


def finish(output):
    return {"action": "finish", "tool_calls": [], "output": output}


def scripted(prompt):
    turn = default_agent_turn(prompt["task"], prompt["context"])
    return turn if turn is not None else finish(CAUSE if prompt["task"] == "cause" else PLAN)


def app_with_agent(tmp_path, behavior=None):
    settings = Settings(_env_file=None, llm_mode="ollama", database_path=tmp_path / "agents.sqlite3", analysis_timeout_seconds=10)
    def handler(request):
        prompt = json.loads(json.loads(request.content)["messages"][1]["content"])
        turn = behavior(prompt) if behavior else None
        return respond(turn if turn is not None else scripted(prompt))
    model = OllamaChatModel(model=settings.ollama_model, base_url=settings.ollama_base_url, transport=httpx.MockTransport(handler))
    app = create_app(settings)
    app.state.pipeline = Pipeline(settings, llm=OllamaAdapter(settings, model=model))
    return app, settings


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_five_agents_use_mcp_and_persist_monitoring_across_restarts(tmp_path):
    app, settings = app_with_agent(tmp_path)
    async with client_for(app) as client:
        response = await client.post('/api/v1/analyses', json=EXAMPLE)
        assert response.status_code == 200, response.text
        result = response.json()
        summary = (await client.get('/api/v1/admin/monitoring')).json()
        assert summary['summary']['completed'] == 1
        assert summary['summary']['input_tokens'] == 960
        assert summary['summary']['output_tokens'] == 320
        assert summary['langsmith']['state'] == 'disabled'
        assert len(summary['agents']) == 5
        item = (await client.get('/api/v1/admin/runs')).json()['items'][0]
        assert item['run_id'] == result['run_id']
        assert item['state'] == 'completed'
        assert item['trace_id'] == result['run_id']
        assert 'measurements' not in json.dumps(item)
        detail = (await client.get(f"/api/v1/admin/runs/{result['run_id']}")).json()
        assert detail['agents'][0]['tools'] == ['get_equipment', 'get_operating_history', 'get_maintenance_history']
        assert detail['agents'][1]['tools'] == ['evaluate_rules', 'compare_history']
        assert detail['agents'][2]['tools'] == ['search_manuals']
        assert detail['agents'][3]['tools'] == ['get_selected_guides']
        assert detail['agents'][4]['decision'] == 'approve'
        assert len(detail['metrics']['llm_calls']) == 8
    async with client_for(create_app(settings)) as restarted:
        saved = (await restarted.get('/api/v1/admin/runs')).json()['items']
        assert saved[0]['run_id'] == result['run_id']


@pytest.mark.parametrize('case', ['wrong_tool', 'premature_finish', 'lower_risk', 'endless_tools'])
async def test_agents_cannot_escape_tools_or_override_rule_and_turn_bounds(tmp_path, case):
    def behavior(prompt):
        stage, ctx = prompt['task'], prompt['context']
        if case == 'wrong_tool' and stage == 'input':
            return {'action': 'tool', 'tool_calls': [{'name': 'get_selected_guides', 'query': None}], 'output': None}
        if case == 'premature_finish' and stage == 'input':
            return finish({'equipment_id': 'P-101', 'next_agent': 'condition'})
        if case == 'lower_risk' and stage == 'condition' and ctx['observations']:
            return finish({'status': '정상', 'missing_fields': [], 'evidence_ids': []})
        if case == 'endless_tools' and stage == 'input':
            return {'action': 'tool', 'tool_calls': [{'name': 'get_equipment', 'query': None}], 'output': None}
    app, _ = app_with_agent(tmp_path, behavior)
    async with client_for(app) as client:
        response = await client.post('/api/v1/analyses', json=EXAMPLE)
        assert response.status_code == 502, response.text
        item = (await client.get('/api/v1/admin/runs')).json()['items'][0]
        assert item['state'] == 'failed' and item['error_code'] == 'INVALID_MODEL_OUTPUT'
        detail = (await client.get(f"/api/v1/admin/runs/{item['run_id']}")).json()
        assert detail['agents'][-1]['decision'] == 'failed'
        assert detail['metrics']['llm_calls']
        if case == 'endless_tools':
            assert detail['agents'][-1]['turns'] == 4


async def test_safety_review_routes_back_and_passes_feedback(tmp_path):
    feedback_seen = []
    def behavior(prompt):
        stage, ctx = prompt['task'], prompt['context']
        if stage == 'safety' and ctx['observations']:
            prior_reviews = [r for r in ctx['handoffs'] if r['agent'] == 'safety']
            if not prior_reviews:
                return finish({'verdict': 'revise_maintenance', 'reason': 'plan_incomplete'})
        if stage == 'maintenance' and ctx.get('review_feedback'):
            feedback_seen.append(ctx['review_feedback'])
    app, _ = app_with_agent(tmp_path, behavior)
    async with client_for(app) as client:
        response = await client.post('/api/v1/analyses', json=EXAMPLE)
        assert response.status_code == 200, response.text
        result = response.json()
        assert [r['agent'] for r in result['agent_reports']] == ['input', 'condition', 'cause', 'maintenance', 'safety', 'maintenance', 'safety']
        assert result['agent_reports'][-2]['attempt'] == 2
        assert result['status'] == '주의'
        assert result['decision_supported'] is False
        assert feedback_seen[0]['reason'] == 'plan_incomplete'
        detail = (await client.get(f"/api/v1/admin/runs/{result['run_id']}")).json()
        assert len(detail['agents']) == 7


@pytest.mark.parametrize('verdict', ['revise_cause', 'reject'])
async def test_safety_rejects_or_stops_repeated_revisions(tmp_path, verdict):
    def behavior(prompt):
        if prompt['task'] == 'safety' and prompt['context']['observations']:
            return finish({'verdict': verdict, 'reason': 'insufficient_evidence'})
    app, _ = app_with_agent(tmp_path, behavior)
    async with client_for(app) as client:
        response = await client.post('/api/v1/analyses/stream', json=EXAMPLE)
        assert 'event: result' not in response.text
        assert 'INVALID_MODEL_OUTPUT' in response.text
        summary = (await client.get('/api/v1/admin/monitoring')).json()['summary']
        assert summary['failed'] == 1


async def test_timeout_is_recorded_as_failed_not_user_cancelled(tmp_path):
    app, settings = app_with_agent(tmp_path)
    settings.analysis_timeout_seconds = 0.05
    class SlowGateway:
        async def call(self, *args):
            await asyncio.sleep(10)
    app.state.gateway = SlowGateway()
    async with client_for(app) as client:
        response = await client.post('/api/v1/analyses', json=EXAMPLE)
        assert response.status_code == 504
        item = (await client.get('/api/v1/admin/runs')).json()['items'][0]
        assert item['state'] == 'failed' and item['error_code'] == 'UPSTREAM_TIMEOUT'


async def test_trace_urls_require_verified_remote_record(tmp_path):
    app, _ = app_with_agent(tmp_path)
    async with client_for(app) as client:
        result = (await client.post('/api/v1/analyses', json=EXAMPLE)).json()
        url = f"/api/v1/admin/runs/{result['run_id']}/trace"
        assert (await client.get(url)).json()['state'] == 'disabled'
        with app.state.pipeline.store.connect() as con:
            con.execute('UPDATE analysis_runs SET tracing_enabled=1 WHERE run_id=?', (result['run_id'],))
        calls = []
        class FakeSmith:
            def read_run(self, trace_id):
                calls.append(trace_id)
                return SimpleNamespace(id=trace_id)
            def get_run_url(self, *, run):
                return f'https://smith.langchain.com/test/r/{run.id}'
        app.state.pipeline.trace_client = FakeSmith()
        trace = (await client.get(url)).json()
        assert trace['state'] == 'available'
        assert trace['url'].endswith(result['run_id'])
        assert calls == [result['run_id']]
        class UnavailableSmith:
            def read_run(self, trace_id):
                raise RuntimeError('PRIVATE_REMOTE_ERROR')
        app.state.pipeline.trace_client = UnavailableSmith()
        trace = (await client.get(url)).json()
        assert trace['state'] == 'unavailable' and trace['url'] is None
        assert 'PRIVATE_REMOTE_ERROR' not in json.dumps(trace)


async def test_monitoring_empty_and_limits(tmp_path):
    app, _ = app_with_agent(tmp_path)
    async with client_for(app) as client:
        assert (await client.get('/api/v1/admin/monitoring')).json()['summary']['runs'] == 0
        assert (await client.get('/api/v1/admin/runs/missing')).status_code == 404
        assert (await client.get('/api/v1/admin/runs/missing/trace')).status_code == 404
        assert (await client.get('/api/v1/admin/runs?limit=101')).status_code == 422


async def test_langsmith_connection_reports_remote_status_without_exposing_key(tmp_path):
    disabled = create_app(Settings(_env_file=None, database_path=tmp_path / 'disabled.sqlite3'))
    async with client_for(disabled) as client:
        assert (await client.get('/api/v1/admin/langsmith/connection')).json()['state'] == 'disabled'

    settings = Settings(_env_file=None, database_path=tmp_path / 'enabled.sqlite3',
                        langsmith_tracing=True, langsmith_api_key='ls-test-only',
                        langsmith_workspace_id='workspace-test')
    enabled = create_app(settings)
    class ConnectedSmith:
        def list_projects(self, *, limit):
            assert limit == 1
            return iter([SimpleNamespace(name='pump-guard')])
    enabled.state.pipeline.trace_client = ConnectedSmith()
    async with client_for(enabled) as client:
        result = (await client.get('/api/v1/admin/langsmith/connection')).json()
        assert result['state'] == 'connected'
        assert 'ls-test-only' not in json.dumps(result)
        class FailedSmith:
            def list_projects(self, *, limit):
                raise RuntimeError('PRIVATE_REMOTE_ERROR')
        enabled.state.pipeline.trace_client = FailedSmith()
        failed = (await client.get('/api/v1/admin/langsmith/connection')).json()
        assert failed['state'] == 'unavailable'
        assert 'PRIVATE_REMOTE_ERROR' not in json.dumps(failed)
