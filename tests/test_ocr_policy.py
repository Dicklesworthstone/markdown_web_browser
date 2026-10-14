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
    REASON_LOCAL_CPU_FALLBACK,
    REASON_LOCAL_GPU_PREFERRED,
    REASON_REEVAL_FAILURE,
    REASON_REEVAL_LATENCY,
    REASON_REEVAL_NOT_REQUIRED,
    REASON_REEVAL_RECOVERED,
    REASON_REEVAL_SUPPRESSED_COOLDOWN,
    REASON_REEVAL_SUPPRESSED_FLAPPING,
    REASON_REEVAL_TIMER,
    REASON_REMOTE_FALLBACK,
    REASON_SKIP_UNHEALTHY,
    select_ocr_backend,
    should_reevaluate_policy,
)


def test_select_ocr_backend_prefers_local_gpu() -> None:
    decision = select_ocr_backend(
        OCRPolicyInputs(
            candidates=(
                OCRBackendCandidate(
                    backend_id="glm-ocr-local-openai",
                    backend_mode="openai-compatible",
                    hardware_path="gpu",
                ),
                OCRBackendCandidate(
                    backend_id="glm-ocr-remote-openai",
                    backend_mode="openai-compatible",
                    hardware_path="remote",
                ),
            )
        )
    )
    assert decision.backend_id == "glm-ocr-local-openai"
    assert REASON_LOCAL_GPU_PREFERRED in decision.reason_codes
    assert decision.fallback_chain == ("glm-ocr-remote-openai",)
    assert decision.reevaluate_after_s == 120


def test_select_ocr_backend_uses_cpu_when_no_gpu_candidate() -> None:
    decision = select_ocr_backend(
        OCRPolicyInputs(
            candidates=(
                OCRBackendCandidate(
                    backend_id="glm-ocr-local-openai",
                    backend_mode="openai-compatible",
                    hardware_path="cpu",
                ),
                OCRBackendCandidate(
                    backend_id="glm-ocr-remote-openai",
                    backend_mode="openai-compatible",
                    hardware_path="remote",
                ),
            )
        )
    )
    assert decision.backend_id == "glm-ocr-local-openai"
    assert REASON_LOCAL_CPU_FALLBACK in decision.reason_codes
    assert decision.reevaluate_after_s == 30


def test_select_ocr_backend_skips_unhealthy_primary() -> None:
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
            )
        )
    )
    assert decision.backend_id == "glm-ocr-remote-openai"
    assert REASON_SKIP_UNHEALTHY in decision.reason_codes
    assert REASON_REMOTE_FALLBACK in decision.reason_codes


def test_select_ocr_backend_requires_candidates() -> None:
    with pytest.raises(ValueError):
        select_ocr_backend(OCRPolicyInputs(candidates=()))


def test_should_reevaluate_policy_signals() -> None:
    decision = OCRPolicyDecision(
        backend_id="glm-ocr-local-openai",
        backend_mode="openai-compatible",
        hardware_path="cpu",
        fallback_chain=("glm-ocr-remote-openai",),
        reason_codes=(REASON_LOCAL_CPU_FALLBACK,),
        reevaluate_after_s=30,
    )
    assert (
        should_reevaluate_policy(
            signal=OCRRuntimeSignal.REQUEST_FAILED, decision=decision
        ).reason_code
        == REASON_REEVAL_FAILURE
    )
    assert (
        should_reevaluate_policy(
            signal=OCRRuntimeSignal.BACKEND_UNHEALTHY, decision=decision
        ).reason_code
        == REASON_REEVAL_FAILURE
    )
    assert (
        should_reevaluate_policy(
            signal=OCRRuntimeSignal.BACKEND_RECOVERED, decision=decision
        ).reason_code
        == REASON_REEVAL_RECOVERED
    )
    assert (
        should_reevaluate_policy(
            signal=OCRRuntimeSignal.LATENCY_SPIKE, decision=decision
        ).reason_code
        == REASON_REEVAL_LATENCY
    )
    assert (
        should_reevaluate_policy(
            signal=OCRRuntimeSignal.PERIODIC_TIMER, decision=decision
        ).reason_code
        == REASON_REEVAL_TIMER
    )
    assert (
        should_reevaluate_policy(signal=OCRRuntimeSignal.NO_CHANGE, decision=decision).reason_code
        == REASON_REEVAL_NOT_REQUIRED
    )


def test_should_reevaluate_policy_suppresses_within_cooldown_window() -> None:
    decision = OCRPolicyDecision(
        backend_id="glm-ocr-local-openai",
        backend_mode="openai-compatible",
        hardware_path="cpu",
        fallback_chain=("glm-ocr-remote-openai",),
        reason_codes=(REASON_LOCAL_CPU_FALLBACK,),
        reevaluate_after_s=30,
    )
    result = should_reevaluate_policy(
        signal=OCRRuntimeSignal.LATENCY_SPIKE,
        decision=decision,
        context=OCRReevaluationContext(
            now_ts=120.0,
            state=OCRPolicyRuntimeState(last_switch_ts=100.0, switch_timestamps=(100.0,)),
            hysteresis=OCRHysteresisSettings(
                cooldown_seconds=45,
                flap_window_seconds=180,
                flap_threshold=3,
            ),
        ),
    )
    assert result.should_reevaluate is False
    assert result.reason_code == REASON_REEVAL_SUPPRESSED_COOLDOWN
    assert result.cooldown_remaining_s == 25
    assert result.state.suppression_count == 1
    assert result.state.cooldown_suppression_count == 1


def test_should_reevaluate_policy_suppresses_flapping() -> None:
    decision = OCRPolicyDecision(
        backend_id="glm-ocr-local-openai",
        backend_mode="openai-compatible",
        hardware_path="cpu",
        fallback_chain=("glm-ocr-remote-openai",),
        reason_codes=(REASON_LOCAL_CPU_FALLBACK,),
        reevaluate_after_s=30,
    )
    result = should_reevaluate_policy(
        signal=OCRRuntimeSignal.PERIODIC_TIMER,
        decision=decision,
        context=OCRReevaluationContext(
            now_ts=220.0,
            state=OCRPolicyRuntimeState(
                last_switch_ts=170.0,
                switch_timestamps=(100.0, 140.0, 170.0),
            ),
            hysteresis=OCRHysteresisSettings(
                cooldown_seconds=0,
                flap_window_seconds=180,
                flap_threshold=3,
            ),
        ),
    )
    assert result.should_reevaluate is False
    assert result.reason_code == REASON_REEVAL_SUPPRESSED_FLAPPING
    assert result.flap_window_count == 3
    assert result.state.suppression_count == 1
    assert result.state.flap_suppression_count == 1


def test_should_reevaluate_policy_hard_failure_bypasses_suppression() -> None:
    decision = OCRPolicyDecision(
        backend_id="glm-ocr-local-openai",
        backend_mode="openai-compatible",
        hardware_path="cpu",
        fallback_chain=("glm-ocr-remote-openai",),
        reason_codes=(REASON_LOCAL_CPU_FALLBACK,),
        reevaluate_after_s=30,
    )
    result = should_reevaluate_policy(
        signal=OCRRuntimeSignal.REQUEST_FAILED,
        decision=decision,
        context=OCRReevaluationContext(
            now_ts=220.0,
            state=OCRPolicyRuntimeState(
                last_switch_ts=210.0,
                switch_timestamps=(100.0, 140.0, 210.0),
            ),
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


def test_declared_model_policy_changes_provider_limits_and_capture_size(monkeypatch):
    from app import ocr_policy
    from app.jobs import _build_capture_config, _build_cache_seed
    from app.schemas import JobCreateRequest
    from app.settings import get_settings

    policies = ocr_policy.load_model_policies()
    default = ocr_policy.resolve_model_policy("olmOCR-2-7B-1025-FP8")
    smaller = ocr_policy.OCRModelPolicy.model_validate(
        {**default.model_dump(), "long_side_px": 640}
    )
    monkeypatch.setattr(
        ocr_policy,
        "load_model_policies",
        lambda: tuple(smaller if p.key == default.key else p for p in policies),
    )
    request = JobCreateRequest(url="https://example.com", ocr_policy=default.key.lower())
    settings = ocr_policy.settings_for_policy(get_settings(), request.ocr_policy)
    config = _build_capture_config(request, settings)
    assert config.long_side_px == 640
    assert settings.ocr.model == default.key and settings.ocr.use_fp8 is True
    fp16 = ocr_policy.settings_for_policy(get_settings(), "olmocr-2-7b-1025")
    assert fp16.ocr.model == "olmOCR-2-7B-1025"
    assert fp16.ocr.max_concurrency <= 6 and fp16.ocr.use_fp8 is False
    other_config = _build_capture_config(
        JobCreateRequest(url=request.url, ocr_policy=fp16.ocr.model), fp16
    )
    assert other_config.long_side_px == 1288
    assert _build_cache_seed(config=config, settings=settings) != _build_cache_seed(
        config=other_config, settings=fp16
    )
    with pytest.raises(ValueError, match="exceeds"):
        _build_capture_config(
            JobCreateRequest(url=request.url, ocr_policy=default.key, long_side_px=1288), settings
        )


@pytest.mark.parametrize("policy", ["unknown", "GLM-OCR-MaaS", "GOT-OCR-2", ""])
def test_unavailable_policies_refused_on_every_request_contract(policy):
    from app.schemas import BatchJobRequest, CrawlRequest, JobCreateRequest, JobReplayRequest

    for model, payload in (
        (JobCreateRequest, {"url": "https://example.com"}),
        (BatchJobRequest, {"urls": ["https://example.com"]}),
        (CrawlRequest, {"url": "https://example.com"}),
        (JobReplayRequest, {}),
    ):
        with pytest.raises(ValueError):
            model.model_validate({**payload, "ocr_policy": policy})


def test_ignored_nested_capture_options_now_refuse():
    from app.schemas import BatchJobRequest, CrawlRequest, JobCreateRequest, JobReplayRequest

    for model, payload in (
        (JobCreateRequest, {"url": "https://example.com"}),
        (BatchJobRequest, {"urls": ["https://example.com"]}),
        (CrawlRequest, {"url": "https://example.com"}),
        (JobReplayRequest, {}),
    ):
        for key, value in (
            ("viewport", {"width": 640}),
            ("ocr", {"model": "unknown"}),
            ("typo", True),
        ):
            with pytest.raises(ValueError, match="Extra inputs"):
                model.model_validate({**payload, key: value})


@pytest.mark.parametrize("content", ["[]", "{}", "x: []", "x: {long_side_px: -1}"])
def test_model_registry_rejects_malformed_definition(tmp_path, content):
    from app.ocr_policy import load_model_policies

    path = tmp_path / "models.yaml"
    path.write_text(content)
    with pytest.raises(ValueError):
        load_model_policies(path)
