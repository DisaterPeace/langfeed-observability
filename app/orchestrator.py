"""
The Orchestrator — the coordinator (platform role).

It owns the run as a whole: it mints ONE session.id, opens each agent owner's
per-session trace, and drives the online feedback loop. It makes no LLM call and
owns no Langfuse project; each agent's LLM traces live in that agent's own project,
joined back to this run by the shared session.id.

Its span `orchestrator.run` is the ROOT of the stream-1 (Tempo) trace: each agent's
OTel spans nest under it, so operations still see one trace per run. It also owns
the run-level metrics (runs.total, run.duration, sessions.active, feedback.escalations)
and applies the "errors don't propagate" rule at the run boundary.
"""

import time
import uuid

from opentelemetry.trace import Status, StatusCode

from app import repo, evaluation
from app.agents import repo_analyzer, bug_detector

# Online feedback loop: if the cheap "quick" pass scores below this recall, the
# orchestrator escalates to the thorough pass. Driven by an eval score MID-RUN.
RECALL_THRESHOLD = 0.9


def run(platform, agent_repo, agent_bug, repo_path: str) -> dict:
    session_id = f"run-{uuid.uuid4().hex[:8]}"
    start = time.perf_counter()

    with platform.tracer.start_as_current_span("orchestrator.run") as span:
        span.set_attribute("session.id", session_id)
        span.set_attribute("run.input", repo_path)
        platform.metrics.sessions_active.add(1)     # this run is now in flight
        outcome = "success"
        try:
            code = repo.read_repo(repo_path)

            # --- Owner 1: repo-analyzer, in its own trace/project ---
            with agent_repo.run_trace(session_id):
                summary = repo_analyzer.analyze(agent_repo, code)

            # --- Owner 2: bug-detector, one trace spanning the feedback loop ---
            with agent_bug.run_trace(session_id):
                # Attempt 1: cheap "quick" pass.
                findings = bug_detector.detect(agent_bug, code, summary, mode="quick", attempt=1)
                recall, _ = evaluation.score_findings(findings)
                span.set_attribute("feedback.recall_initial", round(recall, 3))

                escalated = False
                if recall < RECALL_THRESHOLD:
                    # Observe -> evaluate -> ACT: quick pass wasn't good enough, so
                    # spend the thorough pass and keep whichever result is better.
                    escalated = True
                    findings_thorough = bug_detector.detect(agent_bug, code, summary, mode="thorough", attempt=2)
                    recall_thorough, _ = evaluation.score_findings(findings_thorough)
                    if recall_thorough >= recall:
                        findings, recall = findings_thorough, recall_thorough
                    platform.metrics.feedback_escalations.add(1, {"reason": "low_recall"})

            bug_trace_id = agent_bug.last_trace_id
            span.set_attribute("feedback.triggered", escalated)
            span.set_attribute("feedback.attempts", 2 if escalated else 1)
            span.set_attribute("feedback.recall_final", round(recall, 3))

            span.set_attribute("run.outcome", outcome)
            return {
                "session_id": session_id, "bug_trace_id": bug_trace_id,
                "summary": summary, "findings": findings,
                "escalated": escalated, "recall_final": recall,
            }

        except Exception as e:
            outcome = "failure"
            span.set_attribute("run.outcome", outcome)
            span.set_status(Status(StatusCode.ERROR, str(e)))
            span.record_exception(e)
            raise

        finally:
            duration = time.perf_counter() - start
            platform.metrics.run_duration.record(duration, {"run.outcome": outcome})
            platform.metrics.runs_total.add(1, {"outcome": outcome})
            platform.metrics.sessions_active.add(-1)    # run finished
