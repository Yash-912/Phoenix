"""The only table a mutating action can be dispatched through.

Separate from nodes.TOOL_DISPATCH on purpose. That table is what the LLM's tool
decisions resolve into, so anything in it is reachable from the observer, and the
observer is read-only by the spec's first safety property. Keeping the mutating
actions in their own table is what makes that a property the test suite can
enforce rather than a property the code comments assert.

restart_service is Tier 1 and acts through the proxy. rollback_deployment is
Tier 2 and, by necessity, does not -- replacing a container needs DELETE and
NETWORKS on the socket, which the proxy denies and this phase deliberately did
not widen. Its extra arguments are fixed here rather than at the call site, so
the only thing a caller can influence is the container name.
"""

from collections.abc import Callable

from phoenix.tools import remediation_tool

# Actions taking only a container name.
REMEDIATION_DISPATCH: dict[str, Callable[[str], dict]] = {
    "restart_service": remediation_tool.restart_service,
}

# Actions with a different signature, bound to their fixed extra arguments.
# Rollback receives (service, target_version, rolled_back_from); the version
# comes from deployment history via rollback_target, never from the model, and
# the marker name is provenance for the audit trail.
TIER_2_DISPATCH: dict[str, Callable[..., dict]] = {
    "rollback_deployment": remediation_tool.rollback_deployment,
}


def dispatch(action: str, container: str | None, args: tuple[str, ...] = ()) -> dict:
    """Run `action`, refusing anything not present in one of the two tables."""
    if action in REMEDIATION_DISPATCH:
        return REMEDIATION_DISPATCH[action](container)
    if action in TIER_2_DISPATCH:
        return TIER_2_DISPATCH[action](container, *args)
    return {"status": "error", "error": f"action '{action}' is not dispatchable"}
