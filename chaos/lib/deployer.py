"""Applying a deployment: build a tagged image, recreate the container, record it.

Every artifact switch in the lab goes through `apply_deployment`. The chaos
script uses it to release the bad version, and `rollback_deployment` uses it to
put the good one back. One code path means the rollback cannot drift from the
deploy it is undoing -- if redeploying v17 works, the forward deployment of v18
worked the same way.

**Why this shells out instead of using the socket proxy.** The proxy permits
`GET /containers/*` and `POST /containers/*` only. That is enough to restart a
container but not to replace one: a recreate needs the old container removed and
the new one joined to `phoenix-net`, which are `DELETE` and `NETWORKS`
operations the proxy denies. Rather than widen Docker's permissions -- `DELETE`
on the socket is broad, and handing it to a system that acts on model output is
hard to justify -- the compose CLI is invoked as a subprocess with a fixed
argument list.

**Why that is still a boundary rather than a hole.** The argv is assembled here
from validated inputs and executed with `shell=False`, so no string the caller
supplied is ever interpreted by a shell. Callers cannot pass flags, extra
services, or a compose file: those come from constants or from a fixed
allowlist. `apply_deployment` refuses any service outside `DEPLOYABLE_SERVICES`
and any version outside `KNOWN_VERSIONS`. The set of things a deployment can
touch is therefore decided in this file, not by whoever calls it.

**Images are built, never pulled.** The proxy blocks both, and building locally
is what makes the artifact a real thing: `APP_VERSION` and `REGRESSION_ENABLED`
are compile-time inputs, so v17 and v18 differ in the image rather than in a
runtime flag a restart could clear.

The marker written here records intent plus the digest actually started, so a
reader can tell "we deployed v17" from "v17 is running" and check the second
against the container rather than trusting the first.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from chaos.lib.deploy_tracker import write_deployment_marker

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"

# The lab's compose project, named rather than derived from the directory. Every
# service here has an explicit container_name, so those names are global: if the
# project were derived from the checkout path, a run from a worktree would try to
# create containers the main checkout already owns and fail on a name conflict --
# which is indistinguishable, from the outside, from a rollback that did not work.
COMPOSE_PROJECT = os.environ.get("PHOENIX_COMPOSE_PROJECT", "agentic")

# The only services whose artifact this module may switch. Adding a name here is
# the deliberate act of opting that service into automated deployment changes.
DEPLOYABLE_SERVICES = {"checkout-service"}

# The artifact versions that exist. A rollback to an arbitrary tag would be a
# rollback to something never built or never tested, so the target is drawn from
# history but must still land in this set.
KNOWN_VERSIONS = {"v17", "v18"}

IMAGE_REPO = "agentic/checkout-service"

# The regression is a property of each artifact, recorded so a rollback can
# restore it without having to infer it from the version name.
VERSION_REGRESSION = {"v17": False, "v18": True}
VERSION_COMMIT = {"v17": "good-v17", "v18": "bad-v18"}

DEPLOY_TIMEOUT_SECONDS = 600

# The only services whose runtime config this module may switch, and the only
# keys and values it will set for them. A config rollback does not replace an
# artifact, so it has its own allowlist rather than reusing DEPLOYABLE_SERVICES
# and KNOWN_VERSIONS -- a service could be configurable without being
# redeployable, and conflating the two would let one allowlist's growth loosen
# the other.
CONFIGURABLE_SERVICES = {"auth-service"}

# Maps (service, key) to the compose environment variable that actually reaches
# the container. The container's own variable name (DB_POOL_SIZE) is not reused
# as the compose-level one, because compose only recreates a container when the
# *resolved* environment changes -- naming the host variable identically to the
# container's own default would make "set DB_POOL_SIZE=10" and "do nothing"
# indistinguishable to compose on the one value that matters most, the reset.
CONFIG_ENV_VARS = {
    ("auth-service", "DB_POOL_SIZE"): "AUTH_DB_POOL_SIZE",
}

# The values each key is known to take. A rollback to a value nobody verified
# is the same mistake KNOWN_VERSIONS exists to prevent for images.
CONFIG_KNOWN_VALUES = {
    ("auth-service", "DB_POOL_SIZE"): {"1", "10"},
}


class DeploymentError(RuntimeError):
    """Raised when a deployment is refused or fails. Never swallowed."""


def _compose_env(version: str) -> dict[str, str]:
    """Environment that selects which artifact compose starts.

    These two variables are the entire mechanism of a rollback: compose
    interpolates them into the image tag, the build args, and the container
    label, so one value moves all three together and they cannot disagree.
    """
    env = dict(os.environ)
    env["CHECKOUT_VERSION"] = version
    env["CHECKOUT_REGRESSION"] = "true" if VERSION_REGRESSION.get(version) else "false"
    # The api service needs these to come up at all; a rollback must not be the
    # thing that changes them.
    env.setdefault("POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", ""))
    env.setdefault("PHOENIX_APP_PASSWORD", os.environ.get("PHOENIX_APP_PASSWORD", ""))
    return env


def _compose(*args: str, env: dict[str, str], timeout: int = DEPLOY_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    argv = [
        "docker",
        "compose",
        "-p",
        COMPOSE_PROJECT,
        "-f",
        str(COMPOSE_FILE),
        *args,
    ]
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=str(REPO_ROOT),
        shell=False,
    )


def build_artifact(version: str) -> None:
    """Build `agentic/checkout-service:<version>` with that version baked in."""
    if version not in KNOWN_VERSIONS:
        raise DeploymentError(f"unknown version '{version}'; known: {sorted(KNOWN_VERSIONS)}")
    env = _compose_env(version)
    result = _compose("build", "checkout-service", env=env)
    if result.returncode != 0:
        raise DeploymentError(f"build of {IMAGE_REPO}:{version} failed:\n{result.stderr[-2000:]}")


def _image_lookup(version: str) -> bool | None:
    """Is `agentic/checkout-service:<version>` present locally?

    True or False when docker answered. None when it could not be asked, which is
    deliberately not False: a rollback blocked by an unreadable daemon leaves the
    service broken, and the deploy is still checked afterwards by reading the
    digest and label back off the live container, so an unexpected artifact shows
    up in history rather than being assumed away.
    """
    argv = ["docker", "image", "inspect", f"{IMAGE_REPO}:{version}"]
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=60,
            shell=False,
        )
    except OSError:
        return None
    if result.returncode == 0:
        return True
    # Exit 1 is docker's "no such image". Anything else -- an unknown flag, a
    # dead daemon -- means we did not get an answer, not that the image is gone.
    return False if result.returncode == 1 else None


def apply_deployment(
    service: str,
    version: str,
    *,
    git_commit: str | None = None,
    deployed_by: str = "chaos/deploy.py",
    rolled_back_from: str | None = None,
    build: bool = True,
) -> dict:
    """Deploy `version` to `service` and record what actually started.

    Returns the marker. Raises DeploymentError if the service is not ours to
    deploy, the version is unknown, or compose fails -- a marker is only written
    once the container is up, so history never claims a deployment that did not
    happen.

    `build=False` restores an artifact that must already be present, which is what
    a rollback wants: the tag is what preserves a known-good image, and rebuilding
    it from the current checkout would substitute whatever the checkout is today
    for whatever was actually verified. With the image absent, compose would pull
    one instead, so the deploy stops first.
    """
    if service not in DEPLOYABLE_SERVICES:
        raise DeploymentError(
            f"service '{service}' is not deployable; allowed: {sorted(DEPLOYABLE_SERVICES)}"
        )
    if version not in KNOWN_VERSIONS:
        raise DeploymentError(f"unknown version '{version}'; known: {sorted(KNOWN_VERSIONS)}")

    env = _compose_env(version)
    if build:
        build_artifact(version)
    elif _image_lookup(version) is False:
        raise DeploymentError(
            f"cannot restore {IMAGE_REPO}:{version} because it is not present locally; "
            "a rollback restores a known-good artifact and will not build or pull one"
        )

    # --force-recreate is required: compose would otherwise see the same service
    # definition and leave the old container running, which is exactly the
    # silent no-op that makes a "rollback" claim false.
    result = _compose("up", "-d", "--no-deps", "--force-recreate", service, env=env)
    if result.returncode != 0:
        raise DeploymentError(f"deployment of {version} to {service} failed:\n{result.stderr[-2000:]}")

    digest, label_version = _inspect_container(service)

    return write_deployment_marker(
        service=service,
        image_tag=version,
        git_commit=git_commit or VERSION_COMMIT.get(version, "unknown"),
        config={"regression": VERSION_REGRESSION.get(version, False)},
        deployed_by=deployed_by,
        image_digest=digest,
        rolled_back_from=rolled_back_from,
    ) | {"observed_label_version": label_version}


def apply_config(
    service: str,
    config: dict[str, str],
    *,
    deployed_by: str = "chaos/config_pool.py",
    rolled_back_from: str | None = None,
) -> dict:
    """Set `config`'s keys on `service` and record what was set.

    Unlike `apply_deployment`, this never builds: a config value is a runtime
    input, not a compile-time one, so there is no artifact to produce and
    nothing for `build=False`'s absent-image check to guard against. The only
    thing that can make this fail is compose itself refusing to recreate the
    container, which `_compose`'s returncode check already catches.

    `config` is an entire key/value set rather than one pair so a future
    multi-key config change does not need a new signature, but today's callers
    only ever pass one.
    """
    if service not in CONFIGURABLE_SERVICES:
        raise DeploymentError(
            f"service '{service}' is not configurable; allowed: {sorted(CONFIGURABLE_SERVICES)}"
        )

    env = dict(os.environ)
    for key, value in config.items():
        var = CONFIG_ENV_VARS.get((service, key))
        if var is None:
            raise DeploymentError(f"key '{key}' is not configurable for '{service}'")
        allowed = CONFIG_KNOWN_VALUES.get((service, key), set())
        if value not in allowed:
            raise DeploymentError(
                f"value '{value}' is not a known state for {service}.{key}; "
                f"known: {sorted(allowed)}"
            )
        env[var] = value
    env.setdefault("POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", ""))
    env.setdefault("PHOENIX_APP_PASSWORD", os.environ.get("PHOENIX_APP_PASSWORD", ""))

    # --force-recreate for the same reason apply_deployment uses it: compose
    # resolves the new environment either way, but the running container must
    # actually be replaced for the new value to reach the process, not just be
    # true the next time something else recreates it.
    result = _compose("up", "-d", "--no-deps", "--force-recreate", service, env=env)
    if result.returncode != 0:
        raise DeploymentError(f"config change on {service} failed:\n{result.stderr[-2000:]}")

    return write_deployment_marker(
        service=service,
        image_tag="same",
        git_commit="same",
        config=config,
        deployed_by=deployed_by,
        rolled_back_from=rolled_back_from,
    )


def _inspect_container(service: str) -> tuple[str | None, str | None]:
    """(image digest, app.version label) of the running container.

    Read from the live container rather than from compose, because these are the
    two facts a marker cannot be trusted to supply about itself. The digest is
    what makes a marker checkable: if it disagrees with the marker's image_tag,
    something other than this module changed the service.

    Best-effort by design. A proxy hiccup must not fail a deployment that
    succeeded, so an unreadable container yields (None, None) and the marker
    records that it could not confirm itself.
    """
    try:
        import requests

        url = os.environ.get("DOCKER_PROXY_URL", "http://localhost:2375")
        response = requests.get(f"{url}/containers/{service}/json", timeout=10)
        response.raise_for_status()
        payload = response.json()
    except Exception:  # noqa: BLE001 - resolution is best-effort, never fatal
        return None, None
    labels = (payload.get("Config") or {}).get("Labels") or {}
    return payload.get("Image"), labels.get("app.version")
