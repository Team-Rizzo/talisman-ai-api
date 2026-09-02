"""Mechanism profile: validation and signing, API side.

CRITICAL: the range checks and the signing payload here MUST match
alpharidge-ai/alpharidge_ai/mechanism/profile.py. A profile this side accepts and the
validators reject is a published mechanism nobody is running.

Signing reuses the attestation key validators already verify, so a profile needs no new
trust root.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional

from utils import attestation_crypto as ac

SUPPORTED_SCHEMA_VERSIONS = ("1.2.0",)

SECONDS_PER_BLOCK = 12
DEFAULT_REFRESH_SECONDS = 3600

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


class ProfileError(ValueError):
    """A profile that must not be published. The message is the reason."""


def min_lead_blocks(refresh_seconds: int = DEFAULT_REFRESH_SECONDS) -> int:
    return max(1, int(refresh_seconds) // SECONDS_PER_BLOCK)


def canonical_json(payload: dict) -> str:
    return ac.canonical_json(payload)


def signing_payload(raw: dict) -> str:
    """Everything but the signature, canonically encoded."""
    return canonical_json({k: v for k, v in raw.items() if k != "signature"})


def sign(raw: dict, keypair=None) -> str:
    keypair = keypair or ac.load_signing_key()
    return ac.sign_attestation(keypair, signing_payload(raw))


# ---- range checks -----------------------------------------------------------------

def _num(section: str, d: dict, key: str, lo: float, hi: float,
         *, lo_open: bool = False) -> float:
    if key not in d:
        raise ProfileError(f"{section}.{key} missing")
    try:
        v = float(d[key])
    except (TypeError, ValueError):
        raise ProfileError(f"{section}.{key} not a number: {d[key]!r}")
    if v != v:
        raise ProfileError(f"{section}.{key} is NaN")
    if (v <= lo if lo_open else v < lo) or v > hi:
        raise ProfileError(f"{section}.{key}={v} out of range")
    return v


def _int(section: str, d: dict, key: str, lo: int, hi: int) -> int:
    if key not in d:
        raise ProfileError(f"{section}.{key} missing")
    try:
        v = int(d[key])
    except (TypeError, ValueError):
        raise ProfileError(f"{section}.{key} not an integer: {d[key]!r}")
    if v < lo or v > hi:
        raise ProfileError(f"{section}.{key}={v} out of range")
    return v


def _bool(section: str, d: dict, key: str) -> None:
    if key in d and not isinstance(d[key], bool):
        raise ProfileError(f"{section}.{key} must be true or false")


def _check_settlement(d: dict) -> None:
    _num("settlement", d, "C", 0.0, 1e15, lo_open=True)
    _bool("settlement", d, "floor_gating")
    _bool("settlement", d, "live")


def _check_emission(d: dict) -> None:
    start = _num("emission", d, "bonus_start", 0.0, 1.0)
    full = _num("emission", d, "bonus_full", 0.0, 1.0)
    if full < start:
        raise ProfileError("emission.bonus_full below bonus_start")
    _num("emission", d, "midpoint", 0.0, 1.0)
    _num("emission", d, "gain", 1.0, 50.0)
    _num("emission", d, "ceiling", 0.0, 3.0)
    _int("emission", d, "n_min", 0, 1_000_000)
    _num("emission", d, "ema_alpha", 0.0, 1.0, lo_open=True)


def _check_rations(d: dict) -> None:
    explore = _num("rations", d, "explore", 0.0, 1e6, lo_open=True)
    boost = _num("rations", d, "boost", 0.0, 1e6, lo_open=True)
    if boost < explore:
        raise ProfileError("rations.boost below explore")
    cap = _num("rations", d, "cap", 0.0, 1e9, lo_open=True)
    if cap < explore:
        raise ProfileError("rations.cap below explore")
    _num("rations", d, "probe_day", 1.0, 100.0)
    _num("rations", d, "alpha_day", 0.0, 1.0, lo_open=True)
    _num("rations", d, "slack_target", 0.0, 1.0)
    _num("rations", d, "fill_gate", 0.0, 1.0)
    _int("rations", d, "boost_days", 0, 365)
    _num("rations", d, "boost_tranche_max", 0.0, 1.0)
    _bool("rations", d, "dispatch")


def _check_oracle(d: dict) -> None:
    tiers = d.get("pool_tiers")
    if not isinstance(tiers, (list, tuple)) or not tiers:
        raise ProfileError("oracle.pool_tiers must be a non-empty list")
    if not all(isinstance(t, str) and t for t in tiers):
        raise ProfileError("oracle.pool_tiers must be strings")

    models = d.get("grader_models")
    if not isinstance(models, (list, tuple)) or not models:
        raise ProfileError("oracle.grader_models must be a non-empty list")
    total = 0.0
    for i, m in enumerate(models):
        if not isinstance(m, dict):
            raise ProfileError(f"oracle.grader_models[{i}] must be an object")
        if not isinstance(m.get("id"), str) or not m.get("id"):
            raise ProfileError(f"oracle.grader_models[{i}].id missing")
        total += _num(f"oracle.grader_models[{i}]", m, "weight", 0.0, 1e6)
    if total <= 0:
        raise ProfileError("oracle.grader_models weights sum to zero")

    _num("oracle", d, "keyed_rate_pool", 0.0, 1.0)
    _num("oracle", d, "keyed_rate_keeper", 0.0, 1.0)
    _int("oracle", d, "claim_cap", 1, 10_000)
    _num("oracle", d, "keeper_weight", 0.0, 10.0)
    _int("oracle", d, "schema_cutover_block", 0, 2**63 - 1)
    _bool("oracle", d, "live")


def _check_controller(d: dict) -> None:
    lo = _num("controller", d, "roi_lo", 0.0, 1e6, lo_open=True)
    hi = _num("controller", d, "roi_hi", 0.0, 1e6, lo_open=True)
    if hi <= lo:
        raise ProfileError("controller.roi_hi not above roi_lo")
    _int("controller", d, "arm_days", 1, 365)
    _num("controller", d, "max_step", 0.0, 1.0, lo_open=True)
    _int("controller", d, "gap_days", 1, 365)
    _num("controller", d, "cost_per_point", 0.0, 1e6, lo_open=True)


_SECTIONS = {
    "settlement": _check_settlement,
    "emission": _check_emission,
    "rations": _check_rations,
    "oracle": _check_oracle,
    "controller": _check_controller,
}


def validate(raw: dict, *, current_version: Optional[int] = None,
             refresh_seconds: int = DEFAULT_REFRESH_SECONDS) -> Dict[str, Any]:
    """Check a profile body. Raises ProfileError with the reason."""
    if not isinstance(raw, dict):
        raise ProfileError("profile must be an object")

    schema_version = raw.get("schema_version")
    if not isinstance(schema_version, str) or not _SEMVER.match(schema_version):
        raise ProfileError(f"schema_version malformed: {schema_version!r}")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ProfileError(f"schema_version {schema_version} not supported")

    version = _int("profile", raw, "version", 1, 2**63 - 1)
    publish_block = _int("profile", raw, "publish_block", 0, 2**63 - 1)
    activation_block = _int("profile", raw, "activation_block", 0, 2**63 - 1)

    if current_version is not None and version <= int(current_version):
        raise ProfileError(
            f"version {version} not above the published {current_version}")

    lead = activation_block - publish_block
    required = min_lead_blocks(refresh_seconds)
    if lead < required:
        raise ProfileError(
            f"activation must lead publication by {required} blocks, got {lead}")

    for name, check in _SECTIONS.items():
        section = raw.get(name)
        if not isinstance(section, dict):
            raise ProfileError(f"{name} section missing")
        check(section)

    return raw
