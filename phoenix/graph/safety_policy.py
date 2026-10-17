"""The execution policy: which actions wait for a person, and who may answer.

Two dimensions, kept apart on purpose (PRD section 9). What kind of change an
action is comes from the fix tables in remediation_policy.py. Who may trigger it
comes from here: the execution mode, the approval rule and the approver list.
Nothing in this module reads model output, and nothing the model writes can
reach it. The LLM never sets a mode, an approver or a threshold.

Configuration is environment variables, read once at process start by the
watcher and the API. An unknown value refuses to start rather than falling back
to a looser setting: a typo in PHOENIX_EXECUTION_POLICY must not quietly turn
guarded into lab.

Approval rules have an author floor and a user setting. The floor is the fix
author's choice and the user may only tighten it. So Tier 1 has no floor (it is
auto-approved in guarded mode) and the user may turn approval on for it. Tier 2
has a floor of "approval required" and asking for less is refused at startup.
Tier 3 never reaches an approval: it stops at a pull request, and the set of
things it may do is fixed here.
"""

import os
from dataclasses import dataclass

from phoenix.graph.remediation_policy import TIER_2_ACTIONS

ENV_EXECUTION_POLICY = "PHOENIX_EXECUTION_POLICY"
ENV_APPROVERS = "PHOENIX_APPROVERS"
ENV_APPROVAL_TTL_SECONDS = "PHOENIX_APPROVAL_TTL_SECONDS"
ENV_TIER1_REQUIRES_APPROVAL = "PHOENIX_TIER1_REQUIRES_APPROVAL"
ENV_TIER2_REQUIRES_APPROVAL = "PHOENIX_TIER2_REQUIRES_APPROVAL"

GUARDED = "guarded"
AUTONOMOUS_LAB = "autonomous_lab"
EXECUTION_POLICIES: tuple[str, ...] = (GUARDED, AUTONOMOUS_LAB)
DEFAULT_EXECUTION_POLICY = GUARDED

DEFAULT_APPROVAL_TTL_SECONDS = 900

# Tier 3 is a pull request and nothing past it. This is the whole list of
# GitHub operations a Tier 3 run may perform. Merge, approve and push are not
# on it, and a test holds github_tool to the same list.
TIER3_ALLOWED_ACTIONS: frozenset[str] = frozenset({"open_pull_request", "comment_on_pull_request"})


class PolicyConfigError(ValueError):
    """The configuration is invalid. The process must not start on it."""


@dataclass(frozen=True)
class SafetyConfig:
    execution_policy: str
    approvers: frozenset[str]
    approval_ttl_seconds: int
    tier1_requires_approval: bool


def _parse_bool(name: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise PolicyConfigError(f"{name} must be true or false, not {raw!r}")


def approver_list(env=os.environ) -> frozenset[str]:
    """The approver identities from config. The API and the graph both read this."""
    raw = env.get(ENV_APPROVERS, "")
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def load_config(env=os.environ) -> SafetyConfig:
    """Read and check the safety configuration, or raise PolicyConfigError."""
    policy = env.get(ENV_EXECUTION_POLICY, "").strip() or DEFAULT_EXECUTION_POLICY
    if policy not in EXECUTION_POLICIES:
        raise PolicyConfigError(
            f"{ENV_EXECUTION_POLICY} must be one of {', '.join(EXECUTION_POLICIES)}, not {policy!r}"
        )

    raw_ttl = env.get(ENV_APPROVAL_TTL_SECONDS, "").strip()
    if raw_ttl:
        try:
            ttl = int(raw_ttl)
        except ValueError as exc:
            raise PolicyConfigError(f"{ENV_APPROVAL_TTL_SECONDS} must be a whole number of seconds") from exc
        if ttl <= 0:
            raise PolicyConfigError(f"{ENV_APPROVAL_TTL_SECONDS} must be positive, not {ttl}")
    else:
        ttl = DEFAULT_APPROVAL_TTL_SECONDS

    tier1 = _parse_bool(ENV_TIER1_REQUIRES_APPROVAL, env.get(ENV_TIER1_REQUIRES_APPROVAL, "false") or "false")

    # Tier 2's floor is approval. Accepting "false" here would make the user's
    # setting weaker than the fix author's, which the rule does not allow, so
    # the process refuses to start instead of ignoring the line.
    tier2_raw = env.get(ENV_TIER2_REQUIRES_APPROVAL, "").strip()
    if tier2_raw and _parse_bool(ENV_TIER2_REQUIRES_APPROVAL, tier2_raw) is False:
        raise PolicyConfigError(
            f"{ENV_TIER2_REQUIRES_APPROVAL}=false is weaker than the Tier 2 floor, which "
            "requires approval; remove the setting or set it to true"
        )

    return SafetyConfig(
        execution_policy=policy,
        approvers=approver_list(env),
        approval_ttl_seconds=ttl,
        tier1_requires_approval=tier1,
    )


def action_tier(action: str) -> int:
    """1 for a Tier 1 mitigation, 2 for a Tier 2 recovery. Unknown names are Tier 2.

    An action this table does not place is treated as the stricter tier, so an
    action added to dispatch without a policy entry is held for approval rather
    than run in the open.
    """
    if action in TIER_2_ACTIONS:
        return 2
    if action == "restart_service":
        return 1
    return 2


def approval_required(policy_mode: str, tier: int, config: SafetyConfig) -> bool:
    """Whether an action of this tier waits for an approval under this mode.

    Lab mode is the one place nothing waits. Any mode this function does not
    recognise is treated as guarded, so a value that slipped past validation
    still asks before it acts.
    """
    if policy_mode == AUTONOMOUS_LAB:
        return False
    if tier >= 2:
        return True
    return config.tier1_requires_approval


def assert_tier3_action(action: str) -> None:
    """Refuse any GitHub operation a Tier 3 run is not allowed to perform."""
    if action not in TIER3_ALLOWED_ACTIONS:
        raise PermissionError(f"{action!r} is not a Tier 3 action; Tier 3 stops at a pull request")
