"""The only table a mutating action can be dispatched through.

Separate from nodes.TOOL_DISPATCH on purpose. That table is what the LLM's tool
decisions resolve into, so anything in it is reachable from the observer, and the
observer is read-only by the spec's first safety property. Keeping the mutating
actions in their own table is what makes that a property the test suite can
enforce rather than a property the code comments assert.

One entry, and that is a decision rather than an omission. remediation_tool.py
allowlists pause_worker, resume_worker, and clear_approved_cache and they are
correctly implemented, but CATEGORY_ACTIONS maps no category onto them, so
routing them here would make them reachable actions with nothing that can
justify them. They arrive as a data change when a scenario needs them.
"""

from collections.abc import Callable

from phoenix.tools import remediation_tool

REMEDIATION_DISPATCH: dict[str, Callable[[str], dict]] = {
    "restart_service": remediation_tool.restart_service,
}
