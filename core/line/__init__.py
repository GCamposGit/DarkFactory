"""core.line — DarkFac autonomous production line (HF-27).

Executable pieces that turn a demand into a running product: grill,
planning, development, review, integration, release and reporting. This
package is intentionally thin: `core.line.agent_cli` runs real coding
agent CLIs in write/read mode, and `core.line.routing` chooses which
harness/account to use for each stage under the quota-aware policy
described in `docs/PRODUCTION_LINE_PLAN_2026-09-22.md`. `core.line.workspace`
(HF-27-02) prepares the per-run git worktree on branch `df/<run_id>` that
carries state between stages and hosts. Later HF-27 tickets extend this
package with stage handlers and the human-input channel.
"""

from __future__ import annotations
