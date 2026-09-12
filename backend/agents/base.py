"""
Base classes for the agentic layer.

Every agent is a small class with an `execute(context) -> dict` method.
`context` is a plain dict shared across a pipeline run — each agent reads
what it needs from it and (optionally) writes its outputs back in, so the
next agent in the pipeline can use them without the orchestrator having to
know each agent's internals.

`BaseAgent.run()` wraps `execute()` with timing, error capture, and a
human-readable one-line explanation of what the agent did — this is what
powers the Explainability Agent and the pipeline trace returned to the
frontend.
"""
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class AgentResult:
    agent: str
    success: bool
    data: dict = field(default_factory=dict)
    error: Optional[str] = None
    exception: Optional[BaseException] = None   # the raw exception, so callers
                                                  # can `isinstance()`-check it
                                                  # for HTTP status mapping
    duration_ms: int = 0
    explanation: str = ""


class BaseAgent:
    """Subclass and implement `execute`. Optionally override `explain`."""

    name = "base_agent"
    description = "Base agent — should not be used directly."

    def run(self, context: dict) -> AgentResult:
        start = time.time()
        try:
            data = self.execute(context) or {}
            explanation = self.explain(context, data)
            return AgentResult(
                agent=self.name, success=True, data=data,
                duration_ms=int((time.time() - start) * 1000),
                explanation=explanation,
            )
        except Exception as e:
            return AgentResult(
                agent=self.name, success=False, error=str(e), exception=e,
                duration_ms=int((time.time() - start) * 1000),
            )

    def execute(self, context: dict) -> dict:
        raise NotImplementedError

    def explain(self, context: dict, data: dict) -> str:
        """One-line, non-technical summary of what this agent just did.
        Override for a more specific explanation — this default is a
        fallback so every agent always has *something* to show."""
        return f"{self.name} completed successfully."


def trace_to_dicts(trace):
    """Normalizes a list of AgentResult (or already-plain dicts) into
    plain {agent, summary, success} dicts — used wherever a trace needs
    to be JSON-serialized or handed to the Explainability Agent."""
    out = []
    for r in trace or []:
        if isinstance(r, dict):
            out.append({
                'agent': r.get('agent'),
                'summary': r.get('summary', r.get('explanation', '')),
                'success': r.get('success', True),
            })
        else:
            out.append({
                'agent': getattr(r, 'agent', ''),
                'summary': getattr(r, 'explanation', '') or getattr(r, 'error', ''),
                'success': getattr(r, 'success', True),
            })
    return out
