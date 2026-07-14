"""
The instrumented LLM client — the single place we call the model.

Provider: Ollama Cloud (free tier, $0). It exposes an OpenAI-COMPATIBLE endpoint
at https://ollama.com/v1, so we reuse the standard `openai` client library.

Every call is instrumented on TWO INDEPENDENT STREAMS, now scoped to the CALLING
AGENT (each agent owner is a separate identity + separate Langfuse project):
  Stream 1 — OpenTelemetry: a `chat {model}` span with gen_ai.* attributes plus
             token/cost/latency metrics -> the agent's OTel providers -> Collector.
  Stream 2 — Langfuse SDK: a generation observation under the agent's OPEN trace,
             created with the agent's OWN client -> that agent's Langfuse project.

`generate()` takes the agent's telemetry identity (`agent`) instead of a global
bundle, so the same function serves every owner while keeping their signals apart.

Runs in MOCK mode (canned reply, fake token counts) when OLLAMA_API_KEY is absent.
"""

import os
import time

from dotenv import load_dotenv
from opentelemetry.trace import SpanKind, Status, StatusCode

load_dotenv()  # pull OLLAMA_API_KEY (and everything else) from .env

OLLAMA_BASE_URL = "https://ollama.com/v1"
MODEL = os.getenv("LLM_MODEL", "gpt-oss:20b")

# Privacy control (standard GenAI env var): when false, we do NOT record prompt/
# completion CONTENT — only metadata (model, tokens, latency). Default true for dev.
CAPTURE_CONTENT = os.getenv(
    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "true"
).lower() == "true"

# Ollama Cloud's free tier is $0 (metered by GPU-time quota, not tokens). A $0
# cost panel is useless, so we track an ESTIMATED cost using representative paid
# per-token pricing — real math (tokens x price), with a real price instead of zero.
PRICES = {
    "gpt-oss:20b": {"input": 0.15 / 1_000_000, "output": 0.60 / 1_000_000},
}

_client = None


def _get_client():
    """Lazily build the OpenAI-compatible client. Returns None when no key -> mock mode."""
    global _client
    key = os.getenv("OLLAMA_API_KEY")
    if not key:
        return None
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(base_url=OLLAMA_BASE_URL, api_key=key)
    return _client


def _mock_reply(prompt: str):
    """Canned response + fake token counts so the pipeline runs without a key."""
    text = (
        "[MOCK REPLY] No OLLAMA_API_KEY set, so this is a placeholder. "
        "The tracing/metrics pipeline is exercised exactly as with a real call."
    )
    in_tok = max(1, len(prompt.split()))
    out_tok = max(1, len(text.split()))
    return text, in_tok, out_tok


def generate(agent, *, system: str, prompt: str, model: str = MODEL, max_tokens: int = 1024) -> str:
    """Make one LLM call for `agent`, fully instrumented on BOTH streams.

    Stream 1 (OTel): the `chat {model}` span + gen_ai.* metrics -> Collector.
    Stream 2 (Langfuse): a generation under the agent's open trace -> its project.
    """
    client = _get_client()
    mocked = client is None
    labels = agent.base_labels  # {"agent.id": ...}

    # Stream 2: this owner's generation, created with THIS agent's Langfuse client
    # so it lands in THIS agent's project, nested under its open run trace.
    with agent.lf.start_as_current_observation(as_type="generation", name="llm.generate") as gen:
        # Span name follows the GenAI convention: "{operation} {model}". We pass the
        # agent's captured stream-1 context so this span nests under the agent's OTel
        # span in Tempo (not under the Langfuse trace that is the active context here).
        with agent.tracer.start_as_current_span(
            f"chat {model}", kind=SpanKind.CLIENT, context=getattr(agent, "_otel_ctx", None),
        ) as span:
            span.set_attribute("gen_ai.operation.name", "chat")
            span.set_attribute("gen_ai.system", "ollama")
            span.set_attribute("gen_ai.request.model", model)
            span.set_attribute("gen_ai.request.max_tokens", max_tokens)
            span.set_attribute("agent.id", agent.agent_id)

            t0 = time.perf_counter()
            ttft = None
            try:
                if mocked:
                    text, in_tok, out_tok = _mock_reply(prompt)
                    resp_model, finish = model, "stop"
                    ttft = 0.01
                else:
                    # STREAMING: lets us measure time-to-first-token (TTFT). Ask for
                    # usage in the final chunk via stream_options.
                    stream = client.chat.completions.create(
                        model=model,
                        max_tokens=max_tokens,
                        messages=[
                            {"role": "system", "content": system},
                            {"role": "user", "content": prompt},
                        ],
                        stream=True,
                        stream_options={"include_usage": True},
                        # gpt-oss is a reasoning model; cap reasoning so it doesn't
                        # burn the whole token budget and return empty content.
                        extra_body={"reasoning_effort": "low"},
                    )
                    parts, resp_model, finish, usage = [], model, "stop", None
                    for chunk in stream:
                        if chunk.model:
                            resp_model = chunk.model
                        if chunk.choices:
                            delta = chunk.choices[0].delta
                            if delta and delta.content:
                                if ttft is None:                 # first token arrived
                                    ttft = time.perf_counter() - t0
                                parts.append(delta.content)
                            if chunk.choices[0].finish_reason:
                                finish = chunk.choices[0].finish_reason
                        if getattr(chunk, "usage", None):
                            usage = chunk.usage
                    text = "".join(parts)
                    if usage:
                        in_tok, out_tok = usage.prompt_tokens, usage.completion_tokens
                    else:  # fallback if the provider omits usage on stream
                        in_tok, out_tok = max(1, len(prompt.split())), max(1, len(text.split()))
                duration = time.perf_counter() - t0

                # --- response attributes (GenAI convention) ---
                span.set_attribute("gen_ai.response.model", resp_model)
                span.set_attribute("gen_ai.response.finish_reasons", [finish])
                span.set_attribute("gen_ai.usage.input_tokens", in_tok)
                span.set_attribute("gen_ai.usage.output_tokens", out_tok)
                if ttft is not None:
                    span.set_attribute("gen_ai.server.time_to_first_token", ttft)
                span.set_attribute("llm.mocked", mocked)

                # --- metrics (-> Prometheus, under this agent's identity) ---
                model_labels = {"gen_ai.request.model": model, **labels}
                agent.metrics.llm_tokens.record(in_tok, {"gen_ai.token.type": "input", **model_labels})
                agent.metrics.llm_tokens.record(out_tok, {"gen_ai.token.type": "output", **model_labels})
                agent.metrics.llm_op_duration.record(duration, {"gen_ai.operation.name": "chat", **model_labels})
                if ttft is not None:
                    agent.metrics.llm_ttft.record(ttft, model_labels)
                agent.metrics.llm_finish.add(1, {"finish_reason": finish, **model_labels})
                price = PRICES.get(model, {"input": 0.0, "output": 0.0})
                cost = in_tok * price["input"] + out_tok * price["output"]
                agent.metrics.llm_cost.add(cost, model_labels)
                span.set_attribute("llm.cost.usd", cost)

            except Exception as e:
                # Errors don't auto-propagate — mark THIS span failed explicitly.
                span.set_status(Status(StatusCode.ERROR, str(e)))
                span.record_exception(e)
                raise

        # --- Stream 2: enrich the generation (model, usage, cost, content). ---
        # We hold the `gen` handle directly, so this is deterministic (no reliance
        # on "current observation"); done after the OTel span closed.
        gen.update(
            model=resp_model,
            input={"system": system, "prompt": prompt} if CAPTURE_CONTENT else None,
            output=text if CAPTURE_CONTENT else None,
            usage_details={"input": in_tok, "output": out_tok},
            cost_details={"total": cost},
            metadata={"agent.id": agent.agent_id, "finish_reason": finish,
                      "mocked": mocked, "time_to_first_token_s": ttft},
        )

    return text
