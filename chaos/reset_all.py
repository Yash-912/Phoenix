"""Reset all services to healthy. Usage: python -m chaos.reset_all."""

from __future__ import annotations

import subprocess
import sys

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
    # The consumer's PAUSED flag, set by /chaos/pause. Nothing else clears it.
    ("worker-service", "http://localhost:8004/chaos/resume"),
]

# The containers this lab runs, and so the only ones reset_all will unpause. A paused
# container belonging to anything else on the machine is not ours to touch.
LAB_CONTAINERS = (
    "api-gateway", "auth-service", "checkout-service", "payment-service", "worker-service",
)

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


def _unpause_lab_containers() -> bool:
    """`docker unpause` every lab container that is paused. True when none was left paused.

    A paused container does not answer HTTP, so this runs before any endpoint is called:
    the resets below would otherwise time out against it. Pausing is also an action
    Phoenix itself can take (pause_worker), and nothing else undoes it. Not being able to
    ask docker is a failure, not a clean reset: it cannot be said that nothing is paused.
    """
    try:
        listed = subprocess.run(
            ["docker", "ps", "--filter", "status=paused", "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=30, shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"docker: could not list paused containers: FAILED {exc}")
        return False
    if listed.returncode != 0:
        print(f"docker: could not list paused containers: FAILED {listed.stderr.strip()}")
        return False

    ok = True
    for name in listed.stdout.split():
        if name not in LAB_CONTAINERS:
            continue
        try:
            result = subprocess.run(
                ["docker", "unpause", name], capture_output=True, text=True, timeout=30, shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"{name}: unpause FAILED {exc}")
            ok = False
            continue
        if result.returncode != 0:
            print(f"{name}: unpause FAILED {result.stderr.strip()}")
            ok = False
        else:
            print(f"{name}: unpaused")
    return ok


def main() -> int:
    """Return every service to healthy. 0 when every step worked, 1 when any did not.

    Every step is attempted either way -- one that fails must not leave the rest
    unreset -- but a reset that left something broken must not report success, so the
    failures are counted and the exit status says so.
    """
    failed = False
    if not _unpause_lab_containers():
        failed = True
    for name, url in TARGETS:
        try:
            r = requests.post(url, timeout=5)
            print(f"{name}: {r.status_code} {r.text[:80]}")
            if r.status_code >= 400:
                failed = True
        except requests.RequestException as exc:
            print(f"{name}: FAILED {exc}")
            failed = True
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
                if r.status_code >= 400:
                    failed = True
        except (requests.RequestException, DeploymentError) as exc:
            print(f"{service}: FAILED {exc}")
            failed = True
    try:
        r = requests.post("http://localhost:8002/chaos/slow/disable", timeout=5)
        print(f"auth-service: {r.status_code}")
        if r.status_code >= 400:
            failed = True
    except requests.RequestException as exc:
        print(f"auth-service: FAILED {exc}")
        failed = True

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
        failed = True

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
            failed = True

    if failed:
        print("reset_all: one or more steps FAILED, the lab may not be healthy")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
