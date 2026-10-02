"""Scenario 1: deploy the bad v18 artifact.

Usage: python -m chaos.deploy_bad_v18 [--reset]

This used to flip a process-local flag via POST /chaos/break, which a container
restart silently cleared -- so "restart fixes it" was an artifact of where the
state lived, not a fact about the release. It now deploys a genuinely different
image. The v18 regression is baked into that image, so restarting v18 leaves it
broken and only redeploying v17 restores service. That is the condition Tier 2
exists to detect and reverse.
"""

from __future__ import annotations

import sys

from chaos.lib.deployer import apply_deployment

GOOD_VERSION = "v17"
BAD_VERSION = "v18"


def inject() -> dict:
    marker = apply_deployment(
        service="checkout-service",
        version=BAD_VERSION,
        deployed_by="chaos/deploy_bad_v18.py",
    )
    print(
        f"deployed bad {BAD_VERSION} to checkout-service at {marker['timestamp']} "
        f"(digest={marker['image_digest']}, label={marker['observed_label_version']})"
    )
    return marker


def reset() -> None:
    marker = apply_deployment(
        service="checkout-service",
        version=GOOD_VERSION,
        deployed_by="chaos/deploy_bad_v18.py --reset",
    )
    print(f"reset to healthy {GOOD_VERSION} (label={marker['observed_label_version']})")


if __name__ == "__main__":
    if "--reset" in sys.argv:
        reset()
    else:
        inject()
