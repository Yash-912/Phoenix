"""Scenario 5: ambiguous -- a real, firing incident with no clean cause.

The previous version of this script printed two lines and touched nothing.
That is not an ambiguous incident; it is no incident at all. With no symptom
anywhere, no alert ever fires, no incident row is ever created, and there is
nothing for the agent to even start investigating -- "insufficient evidence,
escalate" was never actually demonstrated, only asserted in a comment.

This version makes payment-service genuinely fail for a bounded window, which
is long enough to trip HighErrorRate (30s `for:`, needs sustained failing
traffic -- nothing else in this lab generates organic load, so the script
generates its own) and short enough that the failure has already self-cleared
by the time a human or the agent looks at it. What makes it ambiguous rather
than Scenario 2/3's overload is deliberate: no deployment marker is written,
no config marker is written, the container never restarts, and the log line
("payment request failed, please retry") matches none of
phoenix.graph.scoring.CATEGORY_KEYWORDS. An agent that investigates this
should find real evidence of failure and nothing that discriminates a cause --
the correct terminal state is the iteration cap, escalated, not a guess.

payment-service, not checkout-service, is the target on purpose. checkout-
service's container always carries an app.version Docker label ("v17" or
"v18"), and "v17"/"v18" are themselves entries in CATEGORY_KEYWORDS["deploy"] --
any evidence call against checkout-service leaks a nonzero deploy signal
regardless of what actually happened, which would contaminate the very
ambiguity this scenario needs. payment-service carries no such label.

Usage: python -m chaos.ambiguous [--reset]
"""

from __future__ import annotations

import sys
import time

import requests

PAYMENT_URL = "http://localhost:8003"
BLIP_SECONDS = 45
REQUEST_INTERVAL_SECONDS = 0.5


def inject() -> None:
    try:
        response = requests.post(
            f"{PAYMENT_URL}/chaos/blip/start",
            params={"duration_seconds": BLIP_SECONDS},
            timeout=5,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"ambiguous: could not start the blip: {exc}")
        return

    print(
        f"ambiguous: payment-service failing generically for {BLIP_SECONDS}s "
        f"(no deploy, no config change, no crash, no category-matching log line)"
    )

    # Nothing else in this lab generates organic traffic, and HighErrorRate's
    # rate() needs real requests to evaluate against -- an alert with no
    # samples never fires, which would silently turn this into the same no-op
    # the previous version was.
    deadline = time.time() + BLIP_SECONDS
    while time.time() < deadline:
        try:
            requests.get(f"{PAYMENT_URL}/charge", timeout=3)
        except requests.RequestException:
            pass
        time.sleep(REQUEST_INTERVAL_SECONDS)

    print("ambiguous: burst complete; the service has already self-recovered.")
    print("Expected agent outcome: insufficient evidence, escalate.")


def reset() -> None:
    try:
        response = requests.post(f"{PAYMENT_URL}/chaos/blip/stop", timeout=5)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"ambiguous: could not force-clear the blip: {exc}")
        return
    print("ambiguous: blip force-cleared")


if __name__ == "__main__":
    if "--reset" in sys.argv:
        reset()
    else:
        inject()
