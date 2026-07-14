"""
Agent owner 2 — Bug Detector.

Takes the analyzer's summary + the raw code and reports likely bugs. It is its OWN
owner: telemetry identity (service.name = agent.id = "bug-detector") and its own
Langfuse project. Supports two modes so the orchestrator can run a cheap first
pass and only escalate to a thorough pass when quality is low (online feedback
loop):

  - "quick"    : fast/cheap — name only the 1-2 most obvious bugs.
  - "thorough" : full review — find every likely defect with reasons.

When the orchestrator escalates, both passes are separate `detect.<mode>`
observations under the SAME per-session bug-detector trace, so one session = one
trace for this owner (with the LLM calls as generations inside).
"""

import time

from app import llm

SYSTEM_QUICK = (
    "You are a fast code reviewer. Name only the 1-2 MOST obvious bugs, one short "
    "line each. Do not be exhaustive; be quick."
)

SYSTEM_THOROUGH = (
    "You are a meticulous code reviewer. Given a codebase and a summary of it, "
    "find ALL likely bugs and defects. For each, give: the file, the problem, and why "
    "it's wrong. Focus on real correctness issues (mutable defaults, wrong math, "
    "unhandled edge cases, exceptions, negative values). Be specific and thorough."
)


def detect(agent, code: str, summary: str, mode: str = "thorough", attempt: int = 1) -> str:
    """Run one bug-detection pass inside the owner's open trace."""
    start = time.perf_counter()
    agent.metrics.invocations.add(1, agent.base_labels)
    system = SYSTEM_QUICK if mode == "quick" else SYSTEM_THOROUGH
    max_tokens = 200 if mode == "quick" else 1024

    try:
        # One observation per pass, tagged with mode/attempt, so an escalated run
        # shows quick + thorough side by side under the single bug-detector trace.
        with agent.lf.start_as_current_observation(as_type="span", name=f"detect.{mode}") as passfx:
            findings = llm.generate(
                agent,
                system=system,
                prompt=f"Codebase summary:\n{summary}\n\nFull source:\n{code}\n\nList the bugs.",
                max_tokens=max_tokens,
            )
            passfx.update(output=findings[:2000], metadata={"mode": mode, "attempt": attempt})
        return findings
    except Exception as e:
        agent.metrics.errors.add(1, {**agent.base_labels, "error.type": type(e).__name__})
        raise
    finally:
        agent.metrics.duration.record(time.perf_counter() - start, agent.base_labels)
