"""Reset all services to healthy. Usage: python -m chaos.reset_all."""

from __future__ import annotations

import requests

TARGETS = [
    ("checkout-service", "http://localhost:8001/chaos/heal"),
    ("payment-service", "http://localhost:8003/chaos/slow/disable"),
    ("worker-service", "http://localhost:8004/chaos/leak/stop"),
    ("api-gateway", "http://localhost:8005/chaos/heal"),
]


def main() -> None:
    for name, url in TARGETS:
        try:
            r = requests.post(url, timeout=5)
            print(f"{name}: {r.status_code} {r.text[:80]}")
        except requests.RequestException as exc:
            print(f"{name}: FAILED {exc}")
    try:
        r = requests.post("http://localhost:8002/chaos/slow/disable", timeout=5)
        print(f"auth-service: {r.status_code}")
    except requests.RequestException as exc:
        print(f"auth-service: FAILED {exc}")


if __name__ == "__main__":
    main()
