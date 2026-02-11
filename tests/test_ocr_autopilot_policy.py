from __future__ import annotations

import pytest

from app.ocr_policy import (
    OCRBackendCandidate,
    OCRHysteresisSettings,
    OCRPolicyDecision,
    OCRPolicyInputs,
    OCRPolicyRuntimeState,
    OCRReevaluationContext,
    OCRRuntimeSignal,
    REASON_LOCAL_GPU_PREFERRED,
    REASON_REEVAL_FAILURE,
    REASON_REEVAL_SUPPRESSED_COOLDOWN,
    REASON_REEVAL_SUPPRESSED_FLAPPING,
    select_ocr_backend,
    should_reevaluate_policy,
)


def _cpu_decision() -> OCRPolicyDecision:
    return OCRPolicyDecision(
        backend_id="glm-ocr-local-openai",
        backend_mode="openai-compatible",
        hardware_path="cpu",
        fallback_chain=("glm-ocr-remote-openai",),
        reason_codes=("policy.local.cpu-fallback",),
        reevaluate_after_s=30,
    )


def test_select_ocr_backend_keeps_fallback_order_after_skips() -> None:
    decision = select_ocr_backend(
        OCRPolicyInputs(
            candidates=(
                OCRBackendCandidate(
                    backend_id="glm-ocr-local-openai",
                    backend_mode="openai-compatible",
                    hardware_path="gpu",
                    healthy=False,
                ),
                OCRBackendCandidate(
                    backend_id="glm-ocr-remote-openai",
                    backend_mode="openai-compatible",
                    hardware_path="remote",
                    healthy=True,
                ),
                OCRBackendCandidate(
                    backend_id="glm-ocr-maas",
                    backend_mode="maas",
                    hardware_path="remote",
                    healthy=True,
                ),
            )
        )
    )
    assert decision.backend_id == "glm-ocr-remote-openai"
    assert decision.fallback_chain == ("glm-ocr-local-openai", "glm-ocr-maas")


@pytest.mark.parametrize(
    ("signal", "now_ts", "expected_reason"),
    [
        (OCRRuntimeSignal.LATENCY_SPIKE, 120.0, REASON_REEVAL_SUPPRESSED_COOLDOWN),
        (OCRRuntimeSignal.PERIODIC_TIMER, 220.0, REASON_REEVAL_SUPPRESSED_FLAPPING),
    ],
)
def test_hysteresis_suppression_reasons_are_stable(
    signal: OCRRuntimeSignal, now_ts: float, expected_reason: str
) -> None:
    state = OCRPolicyRuntimeState(
        last_switch_ts=100.0 if signal == OCRRuntimeSignal.LATENCY_SPIKE else 170.0,
        switch_timestamps=(100.0, 140.0, 170.0),
    )
    settings = OCRHysteresisSettings(cooldown_seconds=45, flap_window_seconds=180, flap_threshold=3)
    result = should_reevaluate_policy(
        signal=signal,
        decision=_cpu_decision(),
        context=OCRReevaluationContext(now_ts=now_ts, state=state, hysteresis=settings),
    )
    assert result.should_reevaluate is False
    assert result.reason_code == expected_reason
    assert result.state.suppression_count == 1


def test_hard_failure_bypasses_hysteresis_and_records_switch() -> None:
    initial = OCRPolicyRuntimeState(
        last_switch_ts=210.0,
        switch_timestamps=(100.0, 140.0, 210.0),
        suppression_count=2,
        cooldown_suppression_count=1,
        flap_suppression_count=1,
    )
    result = should_reevaluate_policy(
        signal=OCRRuntimeSignal.BACKEND_UNHEALTHY,
        decision=_cpu_decision(),
        context=OCRReevaluationContext(
            now_ts=220.0,
            state=initial,
            hysteresis=OCRHysteresisSettings(
                cooldown_seconds=45,
                flap_window_seconds=180,
                flap_threshold=3,
            ),
        ),
    )
    assert result.should_reevaluate is True
    assert result.reason_code == REASON_REEVAL_FAILURE
    assert result.hard_failure_bypass is True
    assert result.state.last_switch_ts == 220.0
    assert result.state.switch_timestamps[-1] == 220.0


def test_gpu_path_still_prefers_longer_reevaluate_interval() -> None:
    decision = select_ocr_backend(
        OCRPolicyInputs(
            candidates=(
                OCRBackendCandidate(
                    backend_id="glm-ocr-local-openai",
                    backend_mode="openai-compatible",
                    hardware_path="gpu",
                    healthy=True,
                ),
                OCRBackendCandidate(
                    backend_id="glm-ocr-remote-openai",
                    backend_mode="openai-compatible",
                    hardware_path="remote",
                    healthy=True,
                ),
            )
        )
    )
    assert REASON_LOCAL_GPU_PREFERRED in decision.reason_codes
    assert decision.reevaluate_after_s == 120
