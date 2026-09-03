"""Publish a mechanism profile.

The only way any economic value changes. Validates the body, signs it with the
attestation key validators already verify, and appends it. There is no edit path and no
delete path: rolling back means publishing the previous body under a higher version, so
the table is the complete record of what the mechanism has ever been.

    python scripts/publish_profile.py --from profile.json --activate-in 7d --dry-run
    python scripts/publish_profile.py --from profile.json --activate-in 7d

Shows what would change and what the fleet looks like before it writes anything. Run it
dry first; the diff against what is live is the point.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prisma import Json, Prisma  # noqa: E402

from utils import mechanism_profile as mp  # noqa: E402

SECONDS_PER_BLOCK = mp.SECONDS_PER_BLOCK
BLOCKS_PER_DAY = 86_400 // SECONDS_PER_BLOCK

# §E14 asks for a week's notice on a capacity change. Warned, never enforced: rollback
# is itself a publish, so a hard minimum would make a bad value uncorrectable for a week.
CAPACITY_NOTICE_DAYS = 7

# The first validator release that resolves profiles. Anything older fetches nothing.
_MIN_VALIDATOR_VERSION = os.getenv("MIN_VALIDATOR_VERSION", "3.6.0")


def _version_tuple(text: str):
    """Compare versions as numbers. Lexicographically "3.10.0" sorts below "3.6.0"."""
    parts = []
    for piece in str(text or "").strip().lstrip("vV").split("."):
        digits = "".join(c for c in piece if c.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts or [0])


def _is_stale(reported: str, minimum: str) -> bool:
    return _version_tuple(reported) < _version_tuple(minimum)

# Switches that change what miners are paid. Called out separately at publish time.
SWITCHES = (("settlement", "live"), ("settlement", "floor_gating"),
            ("rations", "dispatch"), ("oracle", "live"))

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([dhb])$", re.I)


def parse_duration(text: str) -> int:
    """`7d`, `12h` or a raw block count. Days, because nobody thinks in blocks."""
    m = _DURATION.match(str(text).strip())
    if not m:
        try:
            return int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"expected 7d, 12h or a block count, got {text!r}")
    value, unit = float(m.group(1)), m.group(2).lower()
    if unit == "d":
        return int(value * BLOCKS_PER_DAY)
    if unit == "h":
        return int(value * 3600 / SECONDS_PER_BLOCK)
    return int(value)


def chain_block() -> int | None:
    """The current head, read rather than typed. A mistyped block sets the wrong
    activation, and nothing downstream would notice."""
    try:
        import bittensor as bt
        return int(bt.subtensor(network=os.getenv("SUBTENSOR_NETWORK", "finney")).block)
    except Exception as e:
        print(f"could not read the chain head: {e}")
        return None


async def fleet_readiness(prisma, min_version: str | None = None):
    """What the validators are running, from their config polls.

    A profile published to a fleet that cannot parse it is a silent no-op: those
    validators carry on with whatever they had. `min_version` marks each row as stale
    or not so the caller can warn without repeating the comparison.
    """
    try:
        rows = await prisma.query_raw(
            'SELECT validator_hotkey, version, last_seen FROM validator_versions')
    except Exception:
        path = Path(os.getenv("VALIDATOR_VERSIONS_PATH",
                              Path(__file__).resolve().parent.parent
                              / ".validator_versions.json"))
        try:
            data = json.loads(path.read_text())
        except Exception:
            return None
        rows = [{"validator_hotkey": k, **v} for k, v in data.items()]
    if min_version:
        for row in rows:
            row["stale"] = _is_stale(row.get("version"), min_version)
    return rows


def describe_change(body: dict, live: dict | None) -> list[str]:
    """Every economic value that differs from what is live."""
    if not live:
        return ["  (nothing published yet — this is the first profile)"]

    lines = []
    for section in ("settlement", "emission", "rations", "oracle", "controller"):
        for key, value in (body.get(section) or {}).items():
            was = (live.get(section) or {}).get(key)
            if was == value:
                continue
            if isinstance(value, (int, float)) and isinstance(was, (int, float)) and was:
                delta = f"  ({(value / was - 1) * 100:+.1f}%)"
            else:
                delta = ""
            lines.append(f"  {section}.{key}: {was!r} -> {value!r}{delta}")
    return lines or ["  (no economic value differs from what is live)"]


async def publish(path: Path, activate_in: int, published_by: str, dry_run: bool,
                  block: int | None) -> int:
    body = json.loads(path.read_text())

    if block is None:
        block = chain_block()
        if block is None:
            print("REFUSED: no chain head. Pass --chain-block to override.")
            return 1

    prisma = Prisma()
    await prisma.connect()
    try:
        # Paged rather than capped: a run of pending publishes must not hide the row
        # that is actually in force.
        rows, cursor = [], None
        while True:
            page = await prisma.mechanismprofile.find_many(
                order={"version": "desc"}, take=100,
                **({"cursor": {"version": cursor}, "skip": 1} if cursor else {}))
            if not page:
                break
            rows.extend(page)
            if any(r.activationBlock <= int(block) for r in page) or len(page) < 100:
                break
            cursor = page[-1].version
        latest_version = rows[0].version if rows else None
        # What is in force now, not merely the newest row. With a publish already
        # pending, the newest row is the future one, and diffing against it would
        # describe changes nobody is running.
        activated = [r for r in rows if r.activationBlock <= int(block)]
        active = max(activated, key=lambda r: r.version) if activated else None
        live = (active.body or {}) if active else None
        pending = [r for r in rows if r.activationBlock > int(block)]

        body["publish_block"] = int(block)
        body["activation_block"] = int(block) + int(activate_in)
        body["version"] = int(body.get("version") or ((latest_version or 0) + 1))
        body.pop("signature", None)

        try:
            mp.validate(body, current_version=latest_version)
        except mp.ProfileError as e:
            print(f"REFUSED: {e}")
            return 1

        lead = body["activation_block"] - body["publish_block"]
        lead_days = lead / BLOCKS_PER_DAY
        print(f"version          {body['version']}"
              f"{f' (active: {active.version})' if active else ' (none active)'}")
        for row in sorted(pending, key=lambda r: r.activationBlock):
            print(f"  PENDING        v{row.version} activates at {row.activationBlock}")
        print(f"publish block    {body['publish_block']}  (chain head)")
        print(f"activation block {body['activation_block']}  "
              f"(+{lead} blocks, {lead_days:.1f} days)")

        print("\nchanges from what is live:")
        for line in describe_change(body, live):
            print(line)

        on = [f"{s}.{k}" for s, k in SWITCHES if (body.get(s) or {}).get(k)]
        was_on = [f"{s}.{k}" for s, k in SWITCHES
                  if live and (live.get(s) or {}).get(k)]
        if on or was_on:
            print(f"\nswitches         {', '.join(on) if on else 'all off'}")
            newly = [s for s in on if s not in was_on]
            if newly:
                print(f"  TURNING ON:    {', '.join(newly)}")
                print("  These change what miners are paid at the activation block.")

        # Warnings, not refusals. Each is a judgement the operator is entitled to make.
        warnings = []
        capacity_changed = (live and body["settlement"]["C"]
                            != (live.get("settlement") or {}).get("C"))
        if capacity_changed and lead_days < CAPACITY_NOTICE_DAYS:
            warnings.append(
                f"capacity changes with {lead_days:.1f} days of notice; §E14 asks for "
                f"{CAPACITY_NOTICE_DAYS}. Fine for a correction, thin for a step.")

        required_validator = _MIN_VALIDATOR_VERSION
        fleet = await fleet_readiness(prisma, required_validator)
        if fleet is None:
            warnings.append("could not read validator versions; publishing blind to "
                            "what the fleet is running.")
        else:
            print(f"\nfleet            {len(fleet)} validator(s) reporting")
            for row in fleet:
                hotkey = str(row.get("validator_hotkey", "?"))
                print(f"  {hotkey[:12]}..  {row.get('version', '?')}  "
                      f"last seen {row.get('last_seen', '?')}")
            if not fleet:
                warnings.append("no validators have reported a version; a profile they "
                                "cannot fetch changes nothing.")
            stale = [str(r.get("validator_hotkey", "?"))[:12] for r in fleet
                     if _is_stale(r.get("version"), required_validator)]
            if stale:
                warnings.append(
                    f"{len(stale)} validator(s) below {required_validator} "
                    f"({', '.join(stale)}); they may not apply this profile.")

        for w in warnings:
            print(f"\nWARNING: {w}")

        if dry_run:
            print("\ndry run: nothing was published")
            return 0

        if warnings:
            reply = input("\nwarnings above. publish anyway? [y/N] ").strip().lower()
            if reply != "y":
                print("not published")
                return 1

        body["signature"] = mp.sign(body)
        await prisma.mechanismprofile.create(data={
            "version": body["version"],
            "publishBlock": body["publish_block"],
            "activationBlock": body["activation_block"],
            "schemaVersion": body["schema_version"],
            "body": Json(body),
            "signature": body["signature"],
            "publishedBy": published_by,
        })
        print(f"\npublished version {body['version']}, active at block "
              f"{body['activation_block']} (~{lead_days:.1f} days)")
        print("To undo: publish the previous body under a higher version.")
        return 0
    finally:
        await prisma.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="path", required=True, type=Path)
    parser.add_argument("--activate-in", type=parse_duration, required=True,
                        help="7d, 12h, or a block count")
    parser.add_argument("--chain-block", type=int, default=None,
                        help="override the chain head; normally read from the network")
    parser.add_argument("--published-by", default=os.getenv("USER", "operator"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    return asyncio.run(publish(args.path, args.activate_in, args.published_by,
                               args.dry_run, args.chain_block))


if __name__ == "__main__":
    raise SystemExit(main())
