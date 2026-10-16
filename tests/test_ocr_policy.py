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
        should_reevaluate_policy(signal=OCRRuntimeSignal.REQUEST_FAILED, decision=decision).reason_code
        == REASON_REEVAL_FAILURE
    )
    assert (
        should_reevaluate_policy(signal=OCRRuntimeSignal.BACKEND_UNHEALTHY, decision=decision)
        .reason_code
        == REASON_REEVAL_FAILURE
    )
    assert (
        should_reevaluate_policy(signal=OCRRuntimeSignal.BACKEND_RECOVERED, decision=decision)
        .reason_code
        == REASON_REEVAL_RECOVERED
    )
    assert (
        should_reevaluate_policy(signal=OCRRuntimeSignal.LATENCY_SPIKE, decision=decision).reason_code
        == REASON_REEVAL_LATENCY
    )
    assert (
        should_reevaluate_policy(signal=OCRRuntimeSignal.PERIODIC_TIMER, decision=decision).reason_code
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


def test_load_model_catalog_and_resolve_profiles() -> None:
    from app.ocr_policy import (
        PROMPT_TEMPLATE_GENERIC_OCR,
        PROMPT_TEMPLATE_GLM_OCR_DEFAULT,
        PROMPT_TEMPLATE_OLMOCR_V4_YAML,
        load_model_catalog,
        resolve_model_profile,
        resolve_prompt_template,
    )

    catalog = load_model_catalog()
    assert "olmOCR-2-7B-1025-FP8" in catalog
    assert "olmOCR-2-7B-1025" in catalog
    assert "GLM-OCR-MaaS" in catalog
    assert "GLM-OCR-Local-OpenAI" in catalog
    assert "GOT-OCR-2" in catalog
    assert "TextHawk2" in catalog

    fp8 = resolve_model_profile("allenai/olmOCR-2-7B-1025-FP8")
    assert fp8.policy_key == "olmOCR-2-7B-1025-FP8"
    assert fp8.fp8_preferred is True
    assert fp8.max_tiles_in_flight == 8
    assert fp8.long_side_px == 1288
    assert fp8.prompt_template == PROMPT_TEMPLATE_OLMOCR_V4_YAML

    bf16 = resolve_model_profile("olmOCR-2-7B-1025")
    assert bf16.policy_key == "olmOCR-2-7B-1025"
    assert bf16.fp8_preferred is False
    assert bf16.max_tiles_in_flight == 6

    with pytest.raises(ValueError, match="disabled"):
        resolve_model_profile("glm-ocr", allow_disabled=False)

    glm = resolve_model_profile("glm-ocr", allow_disabled=True)
    assert glm.policy_key == "GLM-OCR-Local-OpenAI"
    assert glm.disabled is True
    assert glm.prompt_template == PROMPT_TEMPLATE_GLM_OCR_DEFAULT
    assert glm.max_tiles_in_flight == 8

    with pytest.raises(ValueError, match="disabled"):
        resolve_model_profile("GOT-OCR-2", allow_disabled=False)

    got = resolve_model_profile("GOT-OCR-2", allow_disabled=True)
    assert got.disabled is True
    assert got.long_side_px == 1280
    assert got.prompt_template == PROMPT_TEMPLATE_GENERIC_OCR

    with pytest.raises(ValueError, match="Unknown OCR policy"):
        resolve_model_profile("nonexistent-model-xyz")

    assert resolve_prompt_template("glm-ocr") == PROMPT_TEMPLATE_GLM_OCR_DEFAULT
    assert resolve_prompt_template("GLM-OCR-Local-OpenAI") == PROMPT_TEMPLATE_GLM_OCR_DEFAULT
    assert resolve_prompt_template("olmOCR-2-7B-1025-FP8") == PROMPT_TEMPLATE_OLMOCR_V4_YAML


def test_job_create_request_normalizes_nested_overrides_and_rejects_unknown_fields() -> None:
    from pydantic import ValidationError
    from app.schemas import BatchJobRequest, CrawlRequest, JobCreateRequest

    req = JobCreateRequest(
        url="https://example.com",
        viewport={"width": 1440, "height": 1800, "device_scale_factor": 2, "color_scheme": "dark"},
        ocr={"policy": "olmOCR-2-7B-1025"},
        semantic_post_enabled=True,
        semantic_post_max_chars=12000,
    )
    assert req.viewport_width == 1440
    assert req.viewport_height == 1800
    assert req.color_scheme == "dark"
    assert req.ocr_policy == "olmOCR-2-7B-1025"
    assert req.ocr_model == "olmOCR-2-7B-1025"
    assert req.ocr_use_fp8 is False
    assert req.ocr_max_tiles_in_flight == 6
    assert req.long_side_px == 1288
    assert req.semantic_post_enabled is True
    assert req.semantic_post_max_chars == 12000

    with pytest.raises(ValidationError):
        JobCreateRequest(url="https://example.com", ocr_policy="GOT-OCR-2")

    with pytest.raises(ValidationError):
        JobCreateRequest(url="https://example.com", ocr_policy="GLM-OCR-Local-OpenAI")

    with pytest.raises(ValidationError):
        JobCreateRequest(url="https://example.com", ocr_policy="not-a-model")

    with pytest.raises(ValidationError):
        JobCreateRequest(url="https://example.com", unknown_top_level_field="bad")  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        BatchJobRequest(urls=["https://example.com"], ocr_policy="not-a-model")

    with pytest.raises(ValidationError):
        CrawlRequest(url="https://example.com", ocr_policy="GOT-OCR-2")


def test_settings_ocr_alias_fallback_and_decouple_helpers(tmp_path) -> None:
    from app.settings import (
        get_settings,
        resolve_openai_embedder_settings,
        resolve_semantic_post_settings,
    )

    env_file = tmp_path / ".env.test"
    env_file.write_text(
        "\n".join(
            [
                "OCR_SERVER=https://custom-ocr.example.com/v1",
                "OCR_API_KEY=ocr-alias-secret",
                "OCR_MODEL=olmOCR-2-7B-1025",
                "SEMANTIC_POST_ENABLED=true",
                "SEMANTIC_POST_ENDPOINT=https://llm.example.com/v1",
                "SEMANTIC_POST_MODEL=gpt-4o-mini",
                "SEMANTIC_POST_API_KEY=sem-key",
                "SEMANTIC_POST_MAX_CHARS=9000",
                "OPENAI_BASE_URL=https://embed.example.com/v1",
                "OPENAI_API_KEY=emb-key",
                "OPENAI_EMBEDDING_MODEL=text-embedding-3-small",
            ]
        ),
        encoding="utf-8",
    )

    built = get_settings(str(env_file))
    assert built.ocr.server_url == "https://custom-ocr.example.com/v1"
    assert built.ocr.api_key == "ocr-alias-secret"
    assert built.ocr.model == "olmOCR-2-7B-1025"

    sem = resolve_semantic_post_settings(built)
    assert sem.enabled is True
    assert sem.endpoint == "https://llm.example.com/v1"
    assert sem.model == "gpt-4o-mini"
    assert sem.api_key == "sem-key"
    assert sem.max_chars == 9000

    emb = resolve_openai_embedder_settings(built)
    assert emb.openai_base_url == "https://embed.example.com/v1"
    assert emb.openai_api_key == "emb-key"
    assert emb.openai_embedding_model == "text-embedding-3-small"


