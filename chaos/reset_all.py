"""Reset all services to healthy. Usage: python -m chaos.reset_all."""

from __future__ import annotations

import subprocess

import requests

from chaos import config_pool, deploy_bad_v18, memory_leak, slow_query
from chaos.lib import deploy_tracker
from chaos.lib.deployer import DeploymentError

# checkout-service is absent from this list on purpose. Its fault lives in the
# image, so there is no flag to clear and no endpoint that could clear one --
# asking it to 'heal' reported success while the service stayed broken. main()
# resets it by redeploying v17 through chaos/deploy_bad_v18.py, which is the same
# operation a Tier 2 rollback performs.
TARGETS = [
    ("payment-service", "http://localhost:8003/chaos/blip/stop"),
    ("api-gateway", "http://localhost:8005/chaos/heal"),
]

# Faults whose injection wrote a deployment marker. Clearing the flag alone leaves
# that marker as the newest word on the service's state, so the history keeps
# saying the fault is on -- for an hour, to an investigation that reads it as the
# active configuration. Each one is reverted through its own module, which clears
# the flag and writes the marker that says so: (service, the config key the
# injection set, the module's reset, the endpoint that clears the flag).
RECORDED_FAULTS = [
    ("payment-service", "slow_query", slow_query.reset, "http://localhost:8003/chaos/slow/disable"),
    ("worker-service", "leak", memory_leak.reset, "http://localhost:8004/chaos/leak/stop"),
]


def _fault_on_record(service: str, key: str) -> bool:
    """Whether the newest marker for the service says the fault is on."""
    latest = deploy_tracker.get_recent_deployments(service, 1)
    config = latest[0].get("config") if latest else None
    return bool(isinstance(config, dict) and config.get(key))


def _checkout_version() -> str | None:
    """The app.version label of the running checkout container, or None if unreadable."""
    try:
        result = subprocess.run(
            ["docker", "inspect", "-f", '{{index .Config.Labels "app.version"}}', "checkout-service"],
            capture_output=True, text=True, timeout=30, shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    version = result.stdout.strip()
    return version if result.returncode == 0 and version else None


def main() -> None:
    for name, url in TARGETS:
        try:
            r = requests.post(url, timeout=5)
            print(f"{name}: {r.status_code} {r.text[:80]}")
        except requests.RequestException as exc:
            print(f"{name}: FAILED {exc}")
    for service, key, module_reset, endpoint in RECORDED_FAULTS:
        try:
            if _fault_on_record(service, key):
                module_reset()  # clears the flag and records that it did
                print(f"{service}: {key} reverted and recorded")
            else:
                # Nothing on record to supersede, so a marker would only be a no-op
                # in the history the agent reads. The flag is still cleared.
                r = requests.post(endpoint, timeout=5)
                print(f"{service}: {r.status_code} {r.text[:80]}")
        except requests.RequestException as exc:
            print(f"{service}: FAILED {exc}")
    try:
        r = requests.post("http://localhost:8002/chaos/slow/disable", timeout=5)
        print(f"auth-service: {r.status_code}")
    except requests.RequestException as exc:
        print(f"auth-service: FAILED {exc}")

    # auth-service's DB_POOL_SIZE regression is a config value, not a toggle an
    # endpoint can clear -- the same reason checkout-service is absent from
    # TARGETS above. Reset the same way a Tier 2 rollback does: apply_config
    # back to the known-good value, recreating the container so the value
    # actually takes effect.
    try:
        marker = config_pool.reset()
        print(f"auth-service DB_POOL_SIZE: reset to {marker['config']['DB_POOL_SIZE']}")
    except DeploymentError as exc:
        print(f"auth-service DB_POOL_SIZE: FAILED {exc}")

    # Scenario 1 leaves checkout on the bad image whenever nothing rolls it back.
    # Redeploy the good one only when it is not already running: each redeploy
    # recreates the container and writes a deployment marker, so doing it on every
    # reset would fill the history the agent reads with no-op deploys. A version
    # that cannot be read is redeployed rather than assumed healthy.
    if _checkout_version() != deploy_bad_v18.GOOD_VERSION:
        try:
            deploy_bad_v18.reset()
        except DeploymentError as exc:
            print(f"checkout-service: FAILED {exc}")


if __name__ == "__main__":
    main()
