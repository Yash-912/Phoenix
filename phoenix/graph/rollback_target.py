"""Choosing which artifact to roll back to.

The rollback target is a decision with consequences, so it is made here from
deployment history rather than passed in by a hypothesis. The LLM can say "this
looks like a bad deploy"; it cannot name the version to restore, and nothing in
the graph lets a version string arrive from model output.

**Why "the last healthy deployment" and not "one version earlier".** Rolling
back to whatever preceded the current release would happily restore the release
before *that*, which in a real incident is frequently the broken one. The target
is the newest prior marker that (a) the allowlist recognises as known-good and
(b) was not itself a regression deployment, so the answer degrades safely.

**Why it refuses rather than guesses.** Three cases return no target: no
history, no healthy entry in it, or the running artifact is already the one we
would restore. The last of those matters more than it looks. Scoring keys on the
version appearing in logs and labels, so immediately after a successful rollback
the evidence still names v18 and the deploy hypothesis still scores 0.95. Without
this check the graph would roll back a second time, and a third, treating a
healed service as a fresh casualty. Refusing makes the loop terminate on
verified state instead of on the attempt cap.
"""

from __future__ import annotations

from dataclasses import dataclass

from phoenix.tools.deploy_tool import get_recent_deployments

# The only artifacts a rollback may target. Kept in step with the allowlist in
# remediation_tool.rollback_deployment, which re-checks it independently.
KNOWN_GOOD_VERSIONS = frozenset({"v17"})


@dataclass(frozen=True)
class RollbackTarget:
    """A version to restore, and the marker that says it was good.

    `from_marker` is the deployment being reverted. Carrying it lets the new
    marker name what it undid, so the current/previous relationship is readable
    from either end of the chain rather than only by walking it forward.
    """

    version: str
    from_marker: str | None
    from_version: str | None
    reasoning: str


def resolve_rollback_target(
    service_name: str,
    running_version: str | None = None,
    limit: int = 10,
) -> RollbackTarget | None:
    """Newest known-good version to restore, or None if there is nothing safe.

    `running_version` is the artifact currently observed on the live container.
    Pass it when known: it is what makes the already-healed case detectable.
    """
    result = get_recent_deployments(service_name, limit=limit)
    markers = result.get("deployments") if isinstance(result, dict) else None
    if not markers:
        return None

    from_marker: str | None = None
    from_version: str | None = None
    for index, marker in enumerate(markers):
        tag = marker.get("image_tag")
        if index == 0:
            from_marker = marker.get("timestamp")
            from_version = tag

        if tag not in KNOWN_GOOD_VERSIONS:
            continue
        if marker.get("config", {}).get("regression"):
            continue

        if running_version is not None and running_version == tag:
            return None

        return RollbackTarget(
            version=tag,
            from_marker=from_marker,
            from_version=from_version,
            reasoning=(
                f"newest non-regression artifact in deployment history is {tag}"
                + (f", reverting {from_version} deployed at {from_marker}" if from_version else "")
            ),
        )

    return None
