"""兼容入口：更正候选服务的顶层模块名。"""

from strata.collab.candidates import (  # noqa: F401
    Candidates,
    candidate_row,
    effective_state,
    pending_parts,
    summary,
)
