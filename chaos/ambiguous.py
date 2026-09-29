"""Scenario 5: ambiguous — underdetermined signal, no marker written.

Deliberately writes NOTHING so the agent must conclude insufficient evidence.
Usage: python -m chaos.ambiguous
"""

from __future__ import annotations


def inject() -> None:
    print("ambiguous: no deployment, no config change, no fault injected.")
    print("Expected agent outcome: insufficient evidence, escalate.")


if __name__ == "__main__":
    inject()
