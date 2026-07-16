"""Final result object returned by ``agents.runner.run_agent``.

Originally lived in ``pygen.planner.PlannerResult``. Moved here when the
legacy ``AgentPlanner`` was retired so the LangGraph engine has no
runtime dependency on ``planner.py``.

The class shape (attribute names + types) is intentionally preserved so
the FastAPI layer in ``api.py`` and any downstream code that reads
``result.script_code`` / ``result.tool_calls`` / ``result.iterations``
keeps working unchanged.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class PlannerResult:
    """Result returned by the agent runner.

    Attributes mirror the legacy ``pygen.planner.PlannerResult`` so the
    swap from the legacy engine to LangGraph is transparent to callers.
    """

    def __init__(self):
        self.success: bool = False
        self.script_code: Optional[str] = None
        self.enhanced_analysis: Dict[str, Any] = {}
        self.verified_mapping: Optional[Dict[str, Any]] = None
        self.verified_selectors: Optional[Dict[str, Any]] = None
        self.strategy_summary: str = ""
        self.error: Optional[str] = None
        self.iterations: int = 0
        self.tool_calls: List[Dict[str, Any]] = []
        # ---- persistent-memory bridge (Stage-1 Summary Agent) ----
        self.auto_findings: Optional[Dict[str, Any]] = None
        self.summary_draft_path: Optional[str] = None
        self.html_fingerprint: Optional[str] = None
        # Evidence-driven orchestration outputs.  ``final_state`` is retained
        # in-memory only so the API can append the real runtime result before
        # Stage-1 memory is written.
        self.stage_evidence: Dict[str, Dict[str, Any]] = {}
        self.validation_reports: List[Dict[str, Any]] = []
        self.attribution_decision: Optional[Dict[str, Any]] = None
        self.repair_history: List[Dict[str, Any]] = []
        self.final_state: Dict[str, Any] = {}


__all__ = ["PlannerResult"]
