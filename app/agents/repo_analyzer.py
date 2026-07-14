"""
Agent owner 1 — Repo Analyzer.

Reads the target repo's code and produces a short structural summary, which
becomes context for the bug-detector. It is its OWN owner: its telemetry identity
(service.name = agent.id = "repo-analyzer") and its own Langfuse project.

The orchestrator opens this owner's per-session trace (agent.run_trace); this
module just records the invocation and makes the instrumented LLM call inside it.
"""

import time

from app import llm

SYSTEM = (
    "You are a senior engineer. Summarize the given Python codebase concisely: "
    "list the modules, their main classes/functions, and what each is responsible for. "
    "Keep it under 200 words. Do not look for bugs yet."
)


def analyze(agent, code: str) -> str:
    """Run one repo-analysis invocation inside the owner's open trace."""
    start = time.perf_counter()
    agent.metrics.invocations.add(1, agent.base_labels)
    try:
        summary = llm.generate(
            agent,
            system=SYSTEM,
            prompt=f"Summarize this codebase:\n\n{code}",
        )
        return summary
    except Exception as e:
        agent.metrics.errors.add(1, {**agent.base_labels, "error.type": type(e).__name__})
        raise
    finally:
        agent.metrics.duration.record(time.perf_counter() - start, agent.base_labels)
