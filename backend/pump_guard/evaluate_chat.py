"""Offline resilience evaluation with the imported data; not model accuracy testing.

python -m pump_guard.evaluate_chat
python -m pump_guard.evaluate_chat --live-openai  # optional real model evaluation
python -m pump_guard.evaluate_chat --live-ollama  # optional real model evaluation
"""
import argparse
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
from uuid import uuid4

import httpx

from .config import Settings
from .main import create_app
from .llm import OllamaAdapter, OllamaChatModel
from .models import AnalysisRequest, AnalysisResult
from .pipeline import Pipeline
from .research_data import DATABASE
from .rules import evaluate


async def run(live=False, provider="ollama"):
    rows = []
    with tempfile.TemporaryDirectory() as directory:
        settings = (Settings().model_copy(update={"llm_mode": provider, "database_path": Path(directory) / "evaluation.sqlite3", "analysis_timeout_seconds": 300})
                    if live else Settings(_env_file=None, llm_mode="ollama", database_path=Path(directory) / "evaluation.sqlite3", analysis_timeout_seconds=300))
        app = create_app(settings)
        if not live:
            # Deliberately invalid structured output exercises the actual error path.
            def invalid_output(request):
                return httpx.Response(200, json={"done": True, "done_reason": "stop", "message": {"content": "{}"}})
            model = OllamaChatModel(model=settings.ollama_model, base_url=settings.ollama_base_url, transport=httpx.MockTransport(invalid_output))
            app.state.pipeline = Pipeline(settings, llm=OllamaAdapter(settings, model=model))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=310) as client:
            async def ask(name, session_body, question, expected_text=None):
                created = await client.post('/api/v1/chat/sessions', json=session_body)
                if created.status_code != 201:
                    rows.append({"case": name, "passed": False, "http_status": created.status_code})
                    return
                response = await client.post(f"/api/v1/chat/sessions/{created.json()['id']}/messages", json={"message": question})
                result = response.json()
                sources = {s['id'] for s in result.get('sources', [])}
                passed = response.status_code == 200 and bool(result.get('answer'))
                passed = passed and all(set(section['source_ids']) <= sources for section in result.get('sections', []))
                passed = passed and 'source_ids:' not in result.get('answer', '')
                if expected_text:
                    passed = passed and expected_text in result.get('answer', '')
                rows.append({"case": name, "passed": passed, "http_status": response.status_code,
                             "response_kind": result.get('response_kind'), "source_count": len(sources)})

            base = {"vibration_mm_s": 1.5, "bearing_temperature_c": 62.0, "suction_pressure_bar": 3.0,
                    "discharge_pressure_bar": 6.5, "flow_m3_h": 92.0, "motor_current_a": 41.0}
            for name, mutation in (("normal", {}), ("warning", {"vibration_mm_s": 4.2, "bearing_temperature_c": 82.0}),
                                   ("missing", {"flow_m3_h": None})):
                request = AnalysisRequest(equipment_id="P-101", measurements={**base, **mutation})
                status, missing, findings = evaluate(request, {})
                snapshot = AnalysisResult(run_id=str(uuid4()), equipment_id='P-101', status=status, missing_fields=missing,
                                          findings=findings, candidate_causes=[], checklist=[], safety_notice='')
                app.state.chat.store.save_analysis(snapshot, request.measurements)
                context = {'equipment_id': 'P-101', 'analysis_run_id': snapshot.run_id}
                await ask(f'{name}_explanation', context, '이번 분석에서 왜 이런 판정이 나왔어?', status or '판정 보류')
                await ask(f'{name}_followup', context, '이 분석 결과를 바탕으로 추가적인 점검이 필요한 항목이 있나요?')
                await ask(f'{name}_pressure_scope', context, '흡입 압력이 비정상인것같은데 왜 정상판정을 했지?', '압력이 정상이라는 뜻이 아닙니다')

            if DATABASE.is_file():
                with sqlite3.connect(DATABASE) as db:
                    examples = {}
                    for cycle_id, profile in db.execute('SELECT id,profile FROM cycles ORDER BY id'):
                        examples.setdefault(json.loads(profile)[2], cycle_id)
                for label, cycle in sorted(examples.items()):
                    await ask(f'uci_pump_label_{label}_no_inspections', {'equipment_id': 'UCI-447', 'research_cycle_id': cycle},
                              '최근 점검 기록과 실측 데이터를 보고 상태를 추측해줘', ['누설 없음', '약한 내부 누설', '심한 내부 누설'][label])
                cycle = examples[2]
                saved = await client.post('/api/v1/inspections', json={'equipment_id': 'UCI-447', 'research_cycle_id': cycle,
                    'checks': ['visual'], 'outcome': 'follow_up', 'note': '평가용 임시 점검 기록. 현장 점검 아님.'})
                assert saved.status_code == 201
                await ask('uci_with_temporary_inspection', {'equipment_id': 'UCI-447', 'research_cycle_id': cycle},
                          '최근 점검 기록과 실측 데이터를 보고 상태를 추측해줘', '추가 점검')
    return {'evaluation': f'live_{provider}' if live else 'offline_invalid_llm_resilience',
            'scope': 'HTTP completion, source linkage, pressure scope and experimental-label retrieval; not predictive accuracy or safety validation. P snapshots and inspections are temporary test fixtures; UCI sensor data is imported measured data.',
            'passed': sum(r['passed'] for r in rows), 'total': len(rows), 'cases': rows}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--live-ollama', action='store_true')
    group.add_argument('--live-openai', action='store_true')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[2] / 'research/reports/chat_validation.json')
    args = parser.parse_args()
    report = asyncio.run(run(args.live_ollama or args.live_openai, "openai" if args.live_openai else "ollama"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f"{report['passed']}/{report['total']} passed ({report['evaluation']}) → {args.output}")
    raise SystemExit(0 if report['passed'] == report['total'] else 1)
