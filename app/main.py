"""
Entry point — run the agentic pipeline once against the sample repo.

    .venv\\Scripts\\python -m app.main

Builds the three telemetry identities (orchestrator + two agent owners), runs the
orchestrator, prints results, evaluates, then flushes/shuts down every identity.
"""

import os
import sys

from dotenv import load_dotenv

from app.telemetry import setup_platform, setup_agent
from app import orchestrator, evaluation

load_dotenv()

REPO_PATH = "sample_repo"

# Windows terminals often default to a legacy codepage (e.g. cp1254) that can't
# encode every Unicode character an LLM might use. Force UTF-8 on stdout so
# printing model output never crashes on an unusual character.
sys.stdout.reconfigure(encoding="utf-8")


def main():
    # Three identities, exactly like the platform's Session Manager + N KIOs.
    platform = setup_platform()
    agent_repo = setup_agent(
        "repo-analyzer",
        os.environ["LANGFUSE_REPO_PUBLIC_KEY"], os.environ["LANGFUSE_REPO_SECRET_KEY"],
    )
    agent_bug = setup_agent(
        "bug-detector",
        os.environ["LANGFUSE_BUG_PUBLIC_KEY"], os.environ["LANGFUSE_BUG_SECRET_KEY"],
    )
    try:
        result = orchestrator.run(platform, agent_repo, agent_bug, REPO_PATH)
        print("\n=== RUN:", result["session_id"], "===")
        print("feedback loop:", "ESCALATED to thorough pass" if result["escalated"]
              else "quick pass was good enough (no escalation)")
        print("\n--- Repo summary (repo-analyzer project) ---\n", result["summary"])
        print("\n--- Bug findings (bug-detector project) ---\n", result["findings"])

        # Offline eval: deterministic recall + LLM-as-judge quality, both pushed
        # to the bug-detector's own Langfuse trace.
        recall, per_bug, judge = evaluation.evaluate(agent_bug, result)
        print("\n--- Eval (bug-detector) ---")
        print("  bug_recall (deterministic) = {:.0%}".format(recall))
        if judge is not None:
            print("  llm_judge_quality (LLM-as-judge) = {:.2f}".format(judge))
        for bug_id, found in per_bug:
            print(f"   [{'FOUND' if found else 'MISS '}] {bug_id}")
    finally:
        # Flush BOTH streams for EVERY identity before exit (batch exporters!):
        # each agent flushes its Langfuse client + shuts down its OTel providers.
        agent_repo.shutdown()
        agent_bug.shutdown()
        platform.shutdown()


if __name__ == "__main__":
    main()
