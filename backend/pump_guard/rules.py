from .models import AnalysisRequest, Finding

LABELS = ["정상", "관찰", "주의", "점검 필요"]
# Demonstration thresholds only: NOT ISO, OEM or site-certified limits.
DEMO_LIMITS = {
    "vibration_mm_s": (2.0, 3.5, 5.0),
    "bearing_temperature_c": (70.0, 80.0, 90.0),
}


def evaluate(request: AnalysisRequest, spec: dict):
    values = request.measurements.model_dump()
    missing = [key for key, value in values.items() if value is None]
    findings = []
    for metric, limits in DEMO_LIMITS.items():
        value = values[metric]
        if value is not None:
            level = sum(value >= limit for limit in limits)
            findings.append(Finding(metric=metric, value=value, level=level, reason="demo-v1 예시 상한 비교"))
    # This first step deliberately does not infer risk from an uncalibrated
    # pressure/flow/current model. Missing data must never become 'normal'.
    status = None if missing else LABELS[max((item.level for item in findings), default=0)]
    return status, missing, findings
