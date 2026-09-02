"""API-side profile validation and signing.

The range checks here must agree with the validator's. A profile this side accepts and
the fleet rejects is a published mechanism nobody is running.
"""

import json

import pytest

from utils import mechanism_profile as mp


def valid(version=2, publish_block=9_000, activation_block=10_000) -> dict:
    return {
        "version": version,
        "publish_block": publish_block,
        "activation_block": activation_block,
        "schema_version": "1.2.0",
        "settlement": {"C": 301_743.0},
        "emission": {"midpoint": 0.766, "gain": 15.0, "ceiling": 1.0,
                     "bonus_start": 0.716, "bonus_full": 0.816,
                     "n_min": 100, "ema_alpha": 0.03},
        "rations": {"explore": 25.0, "probe_day": 2.0, "alpha_day": 0.5,
                    "cap": 5000.0, "slack_target": 0.15, "fill_gate": 0.97,
                    "boost": 200.0, "boost_days": 14, "boost_tranche_max": 0.05},
        "oracle": {"pool_tiers": ["number_bearing"], "keyed_rate_pool": 0.9,
                   "keyed_rate_keeper": 0.03, "claim_cap": 40, "keeper_weight": 0.3,
                   "grader_models": [{"id": "model-a", "weight": 0.7},
                                     {"id": "model-b", "weight": 0.3}],
                   "schema_cutover_block": 12_000},
        "controller": {"roi_lo": 1.5, "roi_hi": 6.0, "arm_days": 3,
                       "max_step": 0.2, "gap_days": 14, "cost_per_point": 0.000292},
    }


def test_a_valid_profile_passes():
    assert mp.validate(valid()) is not None


@pytest.mark.parametrize("section,key,bad", [
    ("emission", "gain", 51.0),
    ("emission", "ceiling", 3.5),
    ("settlement", "C", 0.0),
    ("oracle", "keyed_rate_pool", 1.5),
    ("controller", "max_step", 0.0),
    ("rations", "probe_day", 0.5),
])
def test_out_of_range_values_are_refused(section, key, bad):
    body = valid()
    body[section][key] = bad
    with pytest.raises(mp.ProfileError):
        mp.validate(body)


def test_a_missing_section_is_refused():
    body = valid()
    del body["controller"]
    with pytest.raises(mp.ProfileError) as e:
        mp.validate(body)
    assert "controller" in str(e.value)


def test_an_unsupported_schema_is_refused():
    body = valid()
    body["schema_version"] = "2.0.0"
    with pytest.raises(mp.ProfileError):
        mp.validate(body)


def test_a_version_that_does_not_advance_is_refused():
    with pytest.raises(mp.ProfileError) as e:
        mp.validate(valid(version=4), current_version=4)
    assert "not above" in str(e.value)
    mp.validate(valid(version=5), current_version=4)


def test_too_short_a_lead_is_refused():
    with pytest.raises(mp.ProfileError) as e:
        mp.validate(valid(publish_block=9_900, activation_block=10_000))
    assert "lead" in str(e.value)


def test_the_required_lead_covers_a_refresh_interval():
    assert mp.min_lead_blocks(3600) == 300


# ---- signing ----------------------------------------------------------------------

def test_the_signed_payload_excludes_the_signature():
    body = valid()
    body["signature"] = "deadbeef"
    assert "deadbeef" not in mp.signing_payload(body)


def test_the_signed_payload_is_key_order_independent():
    body = valid()
    shuffled = {k: body[k] for k in reversed(list(body))}
    assert mp.signing_payload(body) == mp.signing_payload(shuffled)


def test_the_signed_payload_matches_the_validators_encoding():
    """Both sides sign the same bytes, or every profile is rejected fleet-wide."""
    body = valid()
    expected = json.dumps(body, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False)
    assert mp.signing_payload(body) == expected


def test_a_signature_verifies_against_the_public_key():
    from bittensor_wallet import Keypair
    from utils import attestation_crypto as ac

    keypair = Keypair.create_from_seed("0x" + "11" * 32)
    body = valid()
    signature = mp.sign(body, keypair)
    assert ac.verify_attestation(keypair.ss58_address, mp.signing_payload(body),
                                 signature)


def test_a_tampered_body_fails_verification():
    from bittensor_wallet import Keypair
    from utils import attestation_crypto as ac

    keypair = Keypair.create_from_seed("0x" + "11" * 32)
    body = valid()
    signature = mp.sign(body, keypair)
    body["settlement"]["C"] = 999_999.0
    assert not ac.verify_attestation(keypair.ss58_address, mp.signing_payload(body),
                                     signature)
