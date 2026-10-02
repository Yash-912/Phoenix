"""Choosing which config value to roll back to.

The config equivalent of rollback_target.py: a deployment history that already
exists for image rollbacks also carries config changes (see
chaos/config_pool.py), so the same history answers "what was this key's
last known-good value" without a second storage mechanism.

**Why a value and not a version.** An image rollback restores an artifact that
either exists or does not. A config rollback restores a value that was never
built -- "known good" here means "a value this lab has decided is correct",
declared once in KNOWN_GOOD_CONFIG, not inferred from history the way a
non-regression image tag is. History still matters: it is where the value
actually in force before the regression is found, for the audit trail's
`from_value`/`from_marker` fields, even though the target itself does not
depend on history containing it.

**Why it refuses rather than guesses.** Same two cases as the image rollback:
no history for the service, or the running value already matches the target.
The second one matters for the same reason it does there -- scoring keys on the
regression being visible in evidence, so immediately after a successful config
rollback the evidence can still look like a config-category incident, and
without this check the graph would redeploy the same good value a second and
third time rather than terminating on verified state.
"""

from __future__ import annotations

from dataclasses import dataclass

from phoenix.tools.deploy_tool import get_recent_deployments

# The only value each configurable key is allowed to be rolled back to. Kept in
# step with remediation_tool.rollback_config's own allowlist, which re-checks
# it independently rather than trusting this module to have gotten it right.
KNOWN_GOOD_CONFIG: dict[str, dict[str, str]] = {
    "auth-service": {"DB_POOL_SIZE": "10"},
}


@dataclass(frozen=True)
class ConfigRollbackTarget:
    """A config value to restore, and the marker that last set something else.

    `from_marker`/`from_value` name the deployment being effectively reverted,
    on the same reasoning RollbackTarget carries them: the current/previous
    relationship should be readable from either end without walking history.
    """

    key: str
    value: str
    from_marker: str | None
    from_value: str | None
    reasoning: str


def resolve_config_rollback_target(
    service_name: str,
    key: str,
    running_value: str | None = None,
    limit: int = 10,
) -> ConfigRollbackTarget | None:
    """The known-good value for `key` on `service_name`, or None if there is
    nothing safe to do.

    `running_value` is the value currently observed on the live service. Pass
    it when known: it is what makes the already-healed case detectable.
    """
    good_value = KNOWN_GOOD_CONFIG.get(service_name, {}).get(key)
    if good_value is None:
        return None

    if running_value is not None and running_value == good_value:
        return None

    result = get_recent_deployments(service_name, limit=limit)
    markers = result.get("deployments") if isinstance(result, dict) else None

    from_marker: str | None = None
    from_value: str | None = None
    if markers:
        newest = markers[0]
        from_marker = newest.get("timestamp")
        from_value = (newest.get("config") or {}).get(key)

    return ConfigRollbackTarget(
        key=key,
        value=good_value,
        from_marker=from_marker,
        from_value=from_value,
        reasoning=(
            f"known-good {key} is {good_value}"
            + (f", reverting {from_value} set at {from_marker}" if from_value else "")
        ),
    )
