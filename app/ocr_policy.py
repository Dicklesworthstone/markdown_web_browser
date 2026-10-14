"""Deterministic OCR autopilot policy engine."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from app.settings import Settings


class OCRModelPolicy(BaseModel):
    """Executable limits from the project's declared model inventory."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    key: str
    provider: str
    display_name: str
    long_side_px: int = Field(gt=0, le=16384)
    fp8_preferred: bool
    max_tiles_in_flight: int = Field(gt=0)
    prompt_template: str
    disabled: bool = False
    notes: str = ""
    refs: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)


@lru_cache(maxsize=4)
def load_model_policies(
    path: Path = Path(__file__).resolve().parents[1] / "docs/models.yaml",
) -> tuple[OCRModelPolicy, ...]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload:
        raise ValueError("Model policies must be a nonempty mapping")
    policies = []
    names: set[str] = set()
    for key, values in payload.items():
        if not isinstance(key, str) or not isinstance(values, dict):
            raise ValueError("Each model policy must have a string key and object definition")
        policy = OCRModelPolicy.model_validate({**values, "key": key})
        for name in [key, *policy.aliases]:
            if name.casefold() in names:
                raise ValueError(f"Duplicate model policy name: {name}")
            names.add(name.casefold())
        policies.append(policy)
    return tuple(policies)


def resolve_model_policy(name: str) -> OCRModelPolicy:
    for policy in load_model_policies():
        if name.casefold() in {value.casefold() for value in [policy.key, *policy.aliases]}:
            if policy.disabled:
                raise ValueError(f"OCR policy {policy.key} is disabled")
            if policy.prompt_template != "olmocr_v4":
                raise ValueError(f"OCR prompt {policy.prompt_template} is not implemented")
            return policy
    raise ValueError(f"Unknown OCR policy: {name}")


def validate_ocr_policy(name: str) -> str:
    resolve_model_policy(name)
    return name


def settings_for_policy(settings: Settings, name: str | None) -> Settings:
    if name is None:
        return settings
    policy = resolve_model_policy(name)
    limit = min(settings.ocr.max_concurrency, policy.max_tiles_in_flight)
    return replace(
        settings,
        ocr=replace(
            settings.ocr,
            model=policy.key,
            use_fp8=policy.fp8_preferred,
            min_concurrency=min(settings.ocr.min_concurrency, limit),
            max_concurrency=limit,
        ),
    )


BACKEND_MODE_OPENAI_COMPATIBLE = "openai-compatible"
BACKEND_MODE_MAAS = "maas"

REASON_LOCAL_GPU_PREFERRED = "policy.local.gpu-preferred"
REASON_LOCAL_CPU_FALLBACK = "policy.local.cpu-fallback"
REASON_REMOTE_FALLBACK = "policy.remote.fallback"
REASON_SKIP_UNHEALTHY = "policy.skip.unhealthy"
REASON_REEVAL_TIMER = "policy.reeval.timer"
REASON_REEVAL_FAILURE = "policy.reeval.failure"
REASON_REEVAL_RECOVERED = "policy.reeval.recovered"
REASON_REEVAL_LATENCY = "policy.reeval.latency"
REASON_REEVAL_NOT_REQUIRED = "policy.reeval.not-required"
REASON_REEVAL_SUPPRESSED_COOLDOWN = "policy.reeval.suppressed.cooldown"
REASON_REEVAL_SUPPRESSED_FLAPPING = "policy.reeval.suppressed.flapping"


@dataclass(slots=True, frozen=True)
class OCRBackendCandidate:
    """Candidate backend considered by the autopilot selector."""

    backend_id: str
    backend_mode: str
    hardware_path: str
    healthy: bool | None = None


@dataclass(slots=True, frozen=True)
class OCRPolicyInputs:
    """Input payload used to select an OCR backend deterministically."""

    candidates: tuple[OCRBackendCandidate, ...]


@dataclass(slots=True, frozen=True)
class OCRPolicyDecision:
    """Final OCR backend decision with policy trace metadata."""

    backend_id: str
    backend_mode: str
    hardware_path: str
    fallback_chain: tuple[str, ...]
    reason_codes: tuple[str, ...]
    reevaluate_after_s: int


class OCRRuntimeSignal(str, Enum):
    """Runtime signals that can trigger policy re-evaluation."""

    REQUEST_FAILED = "request_failed"
    BACKEND_UNHEALTHY = "backend_unhealthy"
    BACKEND_RECOVERED = "backend_recovered"
    LATENCY_SPIKE = "latency_spike"
    PERIODIC_TIMER = "periodic_timer"
    NO_CHANGE = "no_change"


@dataclass(slots=True, frozen=True)
class OCRHysteresisSettings:
    """Default anti-flap controls for runtime policy switching."""

    cooldown_seconds: int = 45
    flap_window_seconds: int = 180
    flap_threshold: int = 3


@dataclass(slots=True, frozen=True)
class OCRPolicyRuntimeState:
    """Small state blob for anti-flap suppression accounting."""

    last_switch_ts: float | None = None
    switch_timestamps: tuple[float, ...] = ()
    suppression_count: int = 0
    cooldown_suppression_count: int = 0
    flap_suppression_count: int = 0


@dataclass(slots=True, frozen=True)
class OCRReevaluationContext:
    """Inputs required for deterministic hysteresis/cooldown decisions."""

    now_ts: float
    state: OCRPolicyRuntimeState = field(default_factory=OCRPolicyRuntimeState)
    hysteresis: OCRHysteresisSettings = field(default_factory=OCRHysteresisSettings)


@dataclass(slots=True, frozen=True)
class OCRReevaluationDecision:
    should_reevaluate: bool
    reason_code: str
    state: OCRPolicyRuntimeState
    cooldown_remaining_s: int | None = None
    flap_window_count: int = 0
    hard_failure_bypass: bool = False


def select_ocr_backend(inputs: OCRPolicyInputs) -> OCRPolicyDecision:
    """Select the best backend using explicit GPU/CPU/remote priorities."""

    candidates = list(inputs.candidates)
    if not candidates:
        raise ValueError("OCR policy requires at least one backend candidate")

    reason_codes: list[str] = []
    selected: OCRBackendCandidate | None = None
    for candidate in candidates:
        if candidate.healthy is False:
            reason_codes.append(REASON_SKIP_UNHEALTHY)
            continue
        selected = candidate
        break

    if selected is None:
        selected = candidates[0]
        reason_codes.append(REASON_SKIP_UNHEALTHY)

    if selected.hardware_path == "gpu":
        reason_codes.append(REASON_LOCAL_GPU_PREFERRED)
    elif selected.hardware_path == "cpu":
        reason_codes.append(REASON_LOCAL_CPU_FALLBACK)
    else:
        reason_codes.append(REASON_REMOTE_FALLBACK)

    fallback_chain = tuple(
        candidate.backend_id
        for candidate in candidates
        if candidate.backend_id != selected.backend_id
    )
    # Re-evaluate faster when not on the top-tier local GPU path.
    reevaluate_after_s = 30 if selected.hardware_path != "gpu" else 120

    return OCRPolicyDecision(
        backend_id=selected.backend_id,
        backend_mode=selected.backend_mode,
        hardware_path=selected.hardware_path,
        fallback_chain=fallback_chain,
        reason_codes=tuple(reason_codes),
        reevaluate_after_s=reevaluate_after_s,
    )


def should_reevaluate_policy(
    *,
    signal: OCRRuntimeSignal,
    decision: OCRPolicyDecision,
    context: OCRReevaluationContext | None = None,
) -> OCRReevaluationDecision:
    """Evaluate whether runtime conditions should trigger failover re-selection.

    Hard-failure signals are never blocked by anti-flap suppression.
    """

    base_reason, base_should = _base_reevaluation_reason(signal=signal, decision=decision)
    if context is None:
        state = OCRPolicyRuntimeState()
        if not base_should:
            return OCRReevaluationDecision(False, REASON_REEVAL_NOT_REQUIRED, state=state)
        return OCRReevaluationDecision(True, base_reason, state=state)

    hysteresis = context.hysteresis
    state = _prune_runtime_state(
        context.state,
        now_ts=context.now_ts,
        flap_window_seconds=hysteresis.flap_window_seconds,
    )
    if not base_should:
        return OCRReevaluationDecision(False, REASON_REEVAL_NOT_REQUIRED, state=state)

    hard_failure = signal in {OCRRuntimeSignal.REQUEST_FAILED, OCRRuntimeSignal.BACKEND_UNHEALTHY}
    flap_window_count = len(state.switch_timestamps)
    cooldown_remaining = _cooldown_remaining_seconds(
        state=state,
        now_ts=context.now_ts,
        cooldown_seconds=hysteresis.cooldown_seconds,
    )
    flap_limit_hit = (
        hysteresis.flap_threshold > 0 and flap_window_count >= hysteresis.flap_threshold
    )
    if hard_failure:
        return OCRReevaluationDecision(
            True,
            REASON_REEVAL_FAILURE,
            state=_record_switch(
                state,
                now_ts=context.now_ts,
                flap_window_seconds=hysteresis.flap_window_seconds,
            ),
            flap_window_count=flap_window_count,
            hard_failure_bypass=(cooldown_remaining is not None or flap_limit_hit),
        )

    if cooldown_remaining is not None:
        return OCRReevaluationDecision(
            False,
            REASON_REEVAL_SUPPRESSED_COOLDOWN,
            state=_record_suppression(state, cooldown=True),
            cooldown_remaining_s=cooldown_remaining,
            flap_window_count=flap_window_count,
        )
    if flap_limit_hit:
        return OCRReevaluationDecision(
            False,
            REASON_REEVAL_SUPPRESSED_FLAPPING,
            state=_record_suppression(state, flap=True),
            flap_window_count=flap_window_count,
        )
    return OCRReevaluationDecision(
        True,
        base_reason,
        state=_record_switch(
            state,
            now_ts=context.now_ts,
            flap_window_seconds=hysteresis.flap_window_seconds,
        ),
        flap_window_count=flap_window_count,
    )


def _base_reevaluation_reason(
    *, signal: OCRRuntimeSignal, decision: OCRPolicyDecision
) -> tuple[str, bool]:
    if signal in {OCRRuntimeSignal.REQUEST_FAILED, OCRRuntimeSignal.BACKEND_UNHEALTHY}:
        return REASON_REEVAL_FAILURE, True
    if signal == OCRRuntimeSignal.BACKEND_RECOVERED:
        return REASON_REEVAL_RECOVERED, True
    if signal == OCRRuntimeSignal.LATENCY_SPIKE and decision.hardware_path != "gpu":
        return REASON_REEVAL_LATENCY, True
    if signal == OCRRuntimeSignal.PERIODIC_TIMER:
        return REASON_REEVAL_TIMER, True
    return REASON_REEVAL_NOT_REQUIRED, False


def _prune_runtime_state(
    state: OCRPolicyRuntimeState,
    *,
    now_ts: float,
    flap_window_seconds: int,
) -> OCRPolicyRuntimeState:
    if flap_window_seconds <= 0 or not state.switch_timestamps:
        return state
    cutoff = now_ts - float(flap_window_seconds)
    filtered = tuple(ts for ts in state.switch_timestamps if ts >= cutoff)
    if filtered == state.switch_timestamps:
        return state
    return replace(state, switch_timestamps=filtered)


def _record_switch(
    state: OCRPolicyRuntimeState,
    *,
    now_ts: float,
    flap_window_seconds: int,
) -> OCRPolicyRuntimeState:
    next_state = _prune_runtime_state(
        state,
        now_ts=now_ts,
        flap_window_seconds=flap_window_seconds,
    )
    return replace(
        next_state,
        last_switch_ts=now_ts,
        switch_timestamps=(*next_state.switch_timestamps, now_ts),
    )


def _record_suppression(
    state: OCRPolicyRuntimeState,
    *,
    cooldown: bool = False,
    flap: bool = False,
) -> OCRPolicyRuntimeState:
    return replace(
        state,
        suppression_count=state.suppression_count + 1,
        cooldown_suppression_count=state.cooldown_suppression_count + (1 if cooldown else 0),
        flap_suppression_count=state.flap_suppression_count + (1 if flap else 0),
    )


def _cooldown_remaining_seconds(
    *,
    state: OCRPolicyRuntimeState,
    now_ts: float,
    cooldown_seconds: int,
) -> int | None:
    if cooldown_seconds <= 0:
        return None
    if state.last_switch_ts is None:
        return None
    elapsed = now_ts - state.last_switch_ts
    if elapsed >= cooldown_seconds:
        return None
    return max(1, int(cooldown_seconds - elapsed))
