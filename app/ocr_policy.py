"""Deterministic OCR autopilot policy engine and model catalog resolver."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping

BACKEND_MODE_OPENAI_COMPATIBLE = "openai-compatible"
BACKEND_MODE_MAAS = "maas"

PROMPT_TEMPLATE_OLMOCR_V4 = "olmocr_v4"
PROMPT_TEMPLATE_OLMOCR_V4_YAML = PROMPT_TEMPLATE_OLMOCR_V4
PROMPT_TEMPLATE_GLM_OCR_DEFAULT = "glm_ocr_default"
PROMPT_TEMPLATE_GENERIC_OCR = "generic_ocr"
VALID_PROMPT_TEMPLATES = frozenset(
    {
        PROMPT_TEMPLATE_OLMOCR_V4,
        PROMPT_TEMPLATE_GLM_OCR_DEFAULT,
        PROMPT_TEMPLATE_GENERIC_OCR,
    }
)

DEFAULT_MODELS_YAML_PATH = Path(__file__).resolve().parent.parent / "docs" / "models.yaml"

MODEL_POLICY_ALIASES: dict[str, str] = {
    "default": "olmOCR-2-7B-1025-FP8",
    "olmocr": "olmOCR-2-7B-1025-FP8",
    "olmocr-fp8": "olmOCR-2-7B-1025-FP8",
    "olmocr-2-7b-1025-fp8": "olmOCR-2-7B-1025-FP8",
    "allenai/olmocr-2-7b-1025-fp8": "olmOCR-2-7B-1025-FP8",
    "olmocr-fp16": "olmOCR-2-7B-1025",
    "olmocr-2-7b-1025": "olmOCR-2-7B-1025",
    "allenai/olmocr-2-7b-1025": "olmOCR-2-7B-1025",
    "glm-ocr-maas": "GLM-OCR-MaaS",
    "glm-ocr-local-openai": "GLM-OCR-Local-OpenAI",
    "glm-ocr": "GLM-OCR-Local-OpenAI",
    "glm-4.1v-9b-thinking": "GLM-OCR-Local-OpenAI",
    "thudm/glm-4.1v-9b-thinking": "GLM-OCR-Local-OpenAI",
    "zai-org/glm-4.1v-9b-thinking": "GLM-OCR-Local-OpenAI",
    "got-ocr-2": "GOT-OCR-2",
    "texthawk2": "TextHawk2",
}


@dataclass(slots=True, frozen=True)
class OCRModelProfile:
    """Operational profile for an OCR policy declared in docs/models.yaml."""

    policy_key: str
    provider: str
    display_name: str
    long_side_px: int = 1288
    fp8_preferred: bool = False
    max_tiles_in_flight: int = 8
    prompt_template: str = PROMPT_TEMPLATE_OLMOCR_V4
    disabled: bool = False
    notes: str | None = None
    refs: tuple[str, ...] = ()


def _parse_models_yaml_raw(raw_text: str) -> dict[str, dict[str, Any]]:
    try:
        import yaml  # type: ignore[import-untyped]

        loaded = yaml.safe_load(raw_text)
        if isinstance(loaded, dict):
            return {
                str(k): dict(v)
                for k, v in loaded.items()
                if isinstance(k, str) and isinstance(v, Mapping)
            }
    except Exception:
        pass

    # Deterministic fallback parser for docs/models.yaml's 2-level YAML map
    result: dict[str, dict[str, Any]] = {}
    current_key: str | None = None
    in_refs = False
    for raw_line in raw_text.splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0 and stripped.endswith(":"):
            current_key = stripped[:-1].strip()
            result[current_key] = {}
            in_refs = False
            continue
        if current_key is None:
            continue
        if stripped.startswith("- ") and in_refs:
            val = stripped[2:].strip().strip("\"'")
            refs = result[current_key].setdefault("refs", [])
            if isinstance(refs, list):
                refs.append(val)
            continue
        if ":" in stripped:
            k, v = stripped.split(":", 1)
            k = k.strip()
            v = v.strip()
            if k == "refs" and not v:
                result[current_key]["refs"] = []
                in_refs = True
                continue
            in_refs = False
            if v.lower() in {"true", "false"}:
                result[current_key][k] = v.lower() == "true"
            elif v.isdigit():
                result[current_key][k] = int(v)
            else:
                result[current_key][k] = v.strip("\"'")
    return result


@lru_cache(maxsize=8)
def _load_model_catalog_cached(resolved_path: str) -> dict[str, OCRModelProfile]:
    path = Path(resolved_path)
    if not path.exists():
        raise FileNotFoundError(f"OCR model catalog not found at {path}")
    raw_map = _parse_models_yaml_raw(path.read_text(encoding="utf-8"))
    catalog: dict[str, OCRModelProfile] = {}
    for policy_key, entry in raw_map.items():
        provider = str(entry.get("provider") or "open").strip()
        display_name = str(entry.get("display_name") or policy_key).strip()
        long_side_px = int(entry.get("long_side_px") or 1288)
        if long_side_px < 1:
            raise ValueError(f"Invalid long_side_px for {policy_key}: {long_side_px}")
        fp8_preferred = bool(entry.get("fp8_preferred", False))
        max_tiles_in_flight = int(entry.get("max_tiles_in_flight") or 8)
        if max_tiles_in_flight < 1:
            raise ValueError(
                f"Invalid max_tiles_in_flight for {policy_key}: {max_tiles_in_flight}"
            )
        prompt_template = str(
            entry.get("prompt_template") or PROMPT_TEMPLATE_OLMOCR_V4
        ).strip()
        if prompt_template not in VALID_PROMPT_TEMPLATES:
            raise ValueError(
                f"Invalid prompt_template {prompt_template!r} for {policy_key}; "
                f"expected one of {sorted(VALID_PROMPT_TEMPLATES)}"
            )
        disabled = bool(entry.get("disabled", False))
        notes_raw = entry.get("notes")
        notes = str(notes_raw).strip() if isinstance(notes_raw, str) and notes_raw.strip() else None
        refs_raw = entry.get("refs")
        refs = (
            tuple(str(item).strip() for item in refs_raw if str(item).strip())
            if isinstance(refs_raw, (list, tuple))
            else ()
        )
        catalog[policy_key] = OCRModelProfile(
            policy_key=policy_key,
            provider=provider,
            display_name=display_name,
            long_side_px=long_side_px,
            fp8_preferred=fp8_preferred,
            max_tiles_in_flight=max_tiles_in_flight,
            prompt_template=prompt_template,
            disabled=disabled,
            notes=notes,
            refs=refs,
        )
    return catalog


def load_model_catalog(path: str | Path | None = None) -> dict[str, OCRModelProfile]:
    """Load and validate the OCR model catalog from docs/models.yaml."""

    target = Path(path) if path is not None else DEFAULT_MODELS_YAML_PATH
    return dict(_load_model_catalog_cached(str(target.resolve())))


def resolve_model_profile(
    policy_or_model: str,
    *,
    catalog: Mapping[str, OCRModelProfile] | None = None,
    allow_disabled: bool = False,
) -> OCRModelProfile:
    """Resolve a policy/model key or alias against docs/models.yaml.

    Raises ValueError if the policy is unknown or disabled (when allow_disabled=False).
    """

    if not isinstance(policy_or_model, str) or not policy_or_model.strip():
        raise ValueError("OCR policy/model must be a non-empty string")
    active_catalog = dict(catalog) if catalog is not None else load_model_catalog()
    cleaned = policy_or_model.strip()

    matched: OCRModelProfile | None = active_catalog.get(cleaned)
    if matched is None:
        lower = cleaned.lower()
        alias_target = MODEL_POLICY_ALIASES.get(lower)
        if alias_target and alias_target in active_catalog:
            matched = active_catalog[alias_target]
        else:
            for key, profile in active_catalog.items():
                if key.lower() == lower:
                    matched = profile
                    break

    if matched is None:
        known = ", ".join(sorted(active_catalog))
        raise ValueError(
            f"Unknown OCR policy/model {policy_or_model!r}; declared policies: {known}"
        )

    if matched.disabled and not allow_disabled:
        raise ValueError(
            f"OCR policy {matched.policy_key!r} is disabled in docs/models.yaml"
        )

    return matched


def resolve_prompt_template(
    policy_or_model: str | None,
    *,
    catalog: Mapping[str, OCRModelProfile] | None = None,
) -> str:
    """Resolve the prompt_template for a model or policy, falling back by family."""

    if not policy_or_model:
        return PROMPT_TEMPLATE_OLMOCR_V4
    try:
        profile = resolve_model_profile(
            policy_or_model,
            catalog=catalog,
            allow_disabled=True,
        )
        return profile.prompt_template
    except ValueError:
        lower = (policy_or_model or "").lower()
        if "glm" in lower:
            return PROMPT_TEMPLATE_GLM_OCR_DEFAULT
        if "got-ocr" in lower or "texthawk" in lower:
            return PROMPT_TEMPLATE_GENERIC_OCR
        return PROMPT_TEMPLATE_OLMOCR_V4


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
    flap_limit_hit = hysteresis.flap_threshold > 0 and flap_window_count >= hysteresis.flap_threshold
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
