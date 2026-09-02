"""Publish a mechanism profile.

The only way any economic value changes. Validates the body, signs it with the
attestation key validators already verify, and appends it. There is no edit path and no
delete path: rolling back means publishing the previous body under a higher version, so
the table is the complete record of what the mechanism has ever been.

    python scripts/publish_profile.py --from profile.json --activate-in 7200
    python scripts/publish_profile.py --from profile.json --activate-in 7200 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prisma import Json, Prisma  # noqa: E402

from utils import mechanism_profile as mp  # noqa: E402


async def current_version(prisma: Prisma):
    row = await prisma.mechanismprofile.find_first(order={"version": "desc"})
    return (row.version if row else None), row


async def publish(path: Path, activate_in: int, chain_block: int,
                  published_by: str, dry_run: bool) -> int:
    body = json.loads(path.read_text())

    prisma = Prisma()
    await prisma.connect()
    try:
        latest_version, latest = await current_version(prisma)

        body["publish_block"] = int(chain_block)
        body["activation_block"] = int(chain_block) + int(activate_in)
        body["version"] = int(body.get("version") or ((latest_version or 0) + 1))
        body.pop("signature", None)

        try:
            mp.validate(body, current_version=latest_version)
        except mp.ProfileError as e:
            print(f"REFUSED: {e}")
            return 1

        signature = mp.sign(body)
        body["signature"] = signature

        lead = body["activation_block"] - body["publish_block"]
        print(f"version          {body['version']}"
              f"{f' (previous {latest_version})' if latest_version else ''}")
        print(f"publish block    {body['publish_block']}")
        print(f"activation block {body['activation_block']}  (lead {lead} blocks, "
              f"~{lead * mp.SECONDS_PER_BLOCK / 3600:.1f}h)")
        print(f"capacity         {body['settlement']['C']}")
        if latest:
            previous = (latest.body or {}).get("settlement", {}).get("C")
            if previous:
                change = (body["settlement"]["C"] / float(previous) - 1.0) * 100
                print(f"capacity change  {change:+.1f}%")

        if dry_run:
            print("\ndry run: nothing was published")
            return 0

        await prisma.mechanismprofile.create(data={
            "version": body["version"],
            "publishBlock": body["publish_block"],
            "activationBlock": body["activation_block"],
            "schemaVersion": body["schema_version"],
            "body": Json(body),
            "signature": signature,
            "publishedBy": published_by,
        })
        print("\npublished")
        return 0
    finally:
        await prisma.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="path", required=True, type=Path)
    parser.add_argument("--activate-in", type=int, required=True,
                        help="blocks from the publish block to activation")
    parser.add_argument("--chain-block", type=int, required=True,
                        help="current chain block, recorded as the publish block")
    parser.add_argument("--published-by", default="operator")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    return asyncio.run(publish(args.path, args.activate_in, args.chain_block,
                               args.published_by, args.dry_run))


if __name__ == "__main__":
    raise SystemExit(main())
