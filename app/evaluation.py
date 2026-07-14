"""
Offline evaluation — score a run against the KNOWN planted bugs, then attach the
scores to the BUG-DETECTOR's Langfuse trace (in the bug-detector's own project)
and record them as that owner's metrics.

Evaluation is a quality signal for the bug-detector unit, so everything here is
scoped to that owner: its Langfuse project (via its keys) and its metrics.

`score_findings` is deliberately deterministic: we have a ground-truth answer key
(the bugs we planted in sample_repo), so we just check whether the detector's
findings mention each one. "Recall" = fraction of planted bugs it caught. The
orchestrator also calls it MID-RUN to decide whether to escalate.
"""

import os
import json
import requests
from dotenv import load_dotenv

from app import llm

load_dotenv()

LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "http://localhost:3000")

# The answer key. Each planted bug is "found" if ANY of its keywords appears in
# the findings text (case-insensitive).
EXPECTED_BUGS = [
    {"id": "mutable_default",
     "keywords": ["mutable default", "items=[]", "shared list", "same list",
                  "shared state", "default argument", "default list", "shared across",
                  "shared between", "class-level"]},
    {"id": "discount_math",
     "keywords": ["discount", "/ 100", "/100", "divide by 100", "percent/100",
                  "percentage", "times the full percent", "multiplies", "negative total"]},
    {"id": "div_by_zero",
     "keywords": ["zerodivision", "division by zero", "divide by zero", "divided by zero",
                  "empty cart", "len(cart", "no items", "len == 0", "len(self.items) == 0"]},
    {"id": "sell_keyerror",
     "keywords": ["keyerror", "key error", "does not exist", "doesn't exist",
                  "not exist", "unknown item", "not stocked", "unstocked", "missing key",
                  "not in stock", "not in self.stock", "self.stock[name]", "product is not"]},
    {"id": "negative_stock",
     "keywords": ["negative", "below zero", "below 0", "goes below", "insufficient",
                  "more than available", "goes negative", "negative stock"]},
    {"id": "redundant_true",
     "keywords": ["== true", "redundant", "unnecessary", "just return self.is_low",
                  "compare to true", "boolean comparison", "explicit comparison"]},
]


def score_findings(findings: str):
    """Return (recall_float, per_bug_results list of (id, found_bool))."""
    text = findings.lower()
    results = []
    for bug in EXPECTED_BUGS:
        found = any(k.lower() in text for k in bug["keywords"])
        results.append((bug["id"], found))
    recall = sum(1 for _, found in results if found) / len(EXPECTED_BUGS)
    return recall, results


JUDGE_SYSTEM = (
    "You are an expert code-review evaluator. Given a bug report about a Python "
    "codebase, score its quality from 0.0 to 1.0 based on whether it identifies "
    "real, significant correctness bugs clearly and accurately. Call the score function."
)

_JUDGE_TOOL = [{
    "type": "function",
    "function": {
        "name": "score",
        "description": "Return an evaluation score for the bug report.",
        "parameters": {
            "type": "object",
            "properties": {
                "score": {"type": "number", "description": "Quality from 0.0 to 1.0"},
                "reasoning": {"type": "string", "description": "One-sentence justification"},
            },
            "required": ["score", "reasoning"],
        },
    },
}]


def llm_judge(findings: str, model: str = None):
    """LLM-as-judge: score the bug report 0..1 via a fast tool-calling call.

    Returns (score_float, reasoning) or (None, reason) in mock mode. Reuses the
    same Ollama client as the agents, forcing reasoning_effort=low so the judge is
    fast (~2-3s). The score is pushed to the bug-detector's Langfuse project, so
    Langfuse owns a genuine LLM-based evaluation (not just a deterministic check).
    """
    client = llm._get_client()
    if client is None:
        return None, "mock: no OLLAMA_API_KEY"
    model = model or llm.MODEL
    resp = client.chat.completions.create(
        model=model, max_tokens=400,
        messages=[
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": f"Bug report:\n{findings}\n\nScore it 0.0-1.0."},
        ],
        tools=_JUDGE_TOOL,
        tool_choice={"type": "function", "function": {"name": "score"}},
        extra_body={"reasoning_effort": "low"},  # keep the judge fast
    )
    args = json.loads(resp.choices[0].message.tool_calls[0].function.arguments)
    return float(args["score"]), args.get("reasoning", "")


def push_score_to_langfuse(keys, trace_id: str, name: str, value: float, comment: str = ""):
    """Attach a numeric score to a trace in a SPECIFIC project (via that project's keys)."""
    public, secret = keys
    resp = requests.post(
        f"{LANGFUSE_HOST}/api/public/scores",
        auth=(public, secret),
        json={"traceId": trace_id, "name": name, "value": value,
              "dataType": "NUMERIC", "comment": comment},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def evaluate(agent_bug, result: dict):
    """Score the bug-detector's output two ways; record metrics + push scores to
    the bug-detector's OWN Langfuse project/trace.

    1. bug_recall        — deterministic checklist vs the known planted bugs.
    2. llm_judge_quality — LLM-as-judge score (a real LLM eval).
    """
    findings = result["findings"]
    trace_id = result["bug_trace_id"]

    # --- 1. deterministic recall ---
    recall, per_bug = score_findings(findings)
    found = [b for b, ok in per_bug if ok]
    missed = [b for b, ok in per_bug if not ok]
    agent_bug.metrics.eval_recall.record(recall, {"eval.name": "bug_recall", **agent_bug.base_labels})
    if trace_id:
        push_score_to_langfuse(agent_bug.keys, trace_id, "bug_recall", recall,
                               f"found {len(found)}/{len(per_bug)}: {found}; missed: {missed}")

    # --- 2. LLM-as-judge quality ---
    judge_score = None
    try:
        judge_score, reasoning = llm_judge(findings)
        if judge_score is not None:
            agent_bug.metrics.eval_judge.record(judge_score, {"eval.name": "llm_judge_quality", **agent_bug.base_labels})
            if trace_id:
                push_score_to_langfuse(agent_bug.keys, trace_id, "llm_judge_quality",
                                       judge_score, reasoning[:400])
    except Exception as e:
        print("  (llm_judge skipped:", type(e).__name__, str(e)[:120], ")")

    return recall, per_bug, judge_score
