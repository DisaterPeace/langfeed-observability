"""
Telemetry setup for the multi-owner mirror.

The system has THREE independent telemetry identities, exactly like the platform
contract has a Session Manager + N independent KIOs:

  - one PLATFORM identity  (the orchestrator / coordinator)   -> run-level metrics
  - one AGENT identity per agent owner (repo-analyzer, bug-detector)

Each identity is a *separate* OpenTelemetry resource (its own service.name) with
its own TracerProvider + MeterProvider, so in Prometheus/Grafana every signal is
attributable to the owner that emitted it (job="repo-analyzer" vs "bug-detector"
vs "agent-orchestrator"). All of them push to the SAME shared Collector — the
platform provides one entrypoint; owners never run their own collector.

Each AGENT identity ALSO gets its own Langfuse client pointed at its own Langfuse
project, so an agent's LLM traces land in that owner's project (stream 2). The
orchestrator makes no LLM call and therefore has no Langfuse project.

Two streams, unchanged from before, now multiplied per owner:
  metrics + spans -> OTLP/HTTP -> Collector -> Prometheus / Tempo   (stream 1)
  LLM traces      -> Langfuse SDK -> that agent's Langfuse project   (stream 2)
"""

import os
import atexit
from contextlib import contextmanager

from dotenv import load_dotenv
load_dotenv()  # load .env (incl. OTEL_RESOURCE_ATTRIBUTES) before Resource.create()

from opentelemetry import trace as otel_trace
from opentelemetry.sdk.resources import Resource

# --- traces ---
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

# --- metrics ---
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter

from langfuse import Langfuse, propagate_attributes

# Where the shared Collector listens. Overridable via env so we never hardcode infra.
OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
ENV = os.getenv("DEPLOYMENT_ENV", "dev")

# service.name of every identity in the process. We block ALL of these tracer
# scopes in every Langfuse client so our stream-1 spans can never leak into any
# Langfuse project (belt-and-suspenders: our providers are already non-global).
PLATFORM_SERVICE = "agent-orchestrator"
AGENT_SERVICES = ["repo-analyzer", "bug-detector"]
_BLOCKED_SCOPES = [f"{s}.tracer" for s in [PLATFORM_SERVICE, *AGENT_SERVICES]]


def _providers_for(service_name: str, agent_id: str | None = None):
    """Build a dedicated TracerProvider + MeterProvider for one identity.

    We start from the env-provided resource (service.version, deployment.environment
    from OTEL_RESOURCE_ATTRIBUTES) and OVERRIDE service.name so this identity is
    distinct in every backend. agent.id is added for agents so metrics/traces carry
    the owner key even after service.name is remapped by the Collector.
    """
    attrs = dict(Resource.create().attributes)
    attrs["service.name"] = service_name
    if agent_id:
        attrs["agent.id"] = agent_id
    resource = Resource.create(attrs)

    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{OTLP_ENDPOINT}/v1/traces"))
    )
    metric_reader = PeriodicExportingMetricReader(
        OTLPMetricExporter(endpoint=f"{OTLP_ENDPOINT}/v1/metrics"),
        export_interval_millis=5000,
    )
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    return tracer_provider, meter_provider


# ---------------------------------------------------------------------------
# Instruments
# ---------------------------------------------------------------------------
class PlatformInstruments:
    """Run-level metrics owned by the orchestrator (the session-level view)."""

    def __init__(self, meter):
        self.runs_total = meter.create_counter(
            "runs.total", unit="1",
            description="Completed runs, tagged by outcome (success/failure)",
        )
        self.run_duration = meter.create_histogram(
            "run.duration", unit="s",
            description="Wall-clock duration of a full orchestrator run",
        )
        # Mandatory baseline concurrency gauge: +1 when a run starts, -1 when it ends.
        self.sessions_active = meter.create_up_down_counter(
            "sessions.active", unit="1",
            description="In-flight runs/sessions (mandatory baseline concurrency gauge)",
        )
        self.feedback_escalations = meter.create_counter(
            "feedback.escalations", unit="1",
            description="Times the online loop escalated to a thorough pass",
        )


class AgentInstruments:
    """Per-owner metrics. One set per agent identity; every series carries the
    owner's service.name (job) plus an explicit agent.id label, mirroring the
    contract's 'baseline metrics, each labelled with kio.id'."""

    def __init__(self, meter):
        # --- baseline per-unit metrics ---
        self.invocations = meter.create_counter(
            "agent.invocations", unit="1",
            description="Times this agent was invoked (this unit's request count)",
        )
        self.duration = meter.create_histogram(
            "agent.duration", unit="s",
            description="Duration of a single agent invocation",
        )
        self.errors = meter.create_counter(
            "agent.errors", unit="1",
            description="Agent invocations that raised, by error.type",
        )
        # --- GenAI semantic-convention metrics (this unit's LLM usage) ---
        self.llm_tokens = meter.create_histogram(
            "gen_ai.client.token.usage", unit="{token}",
            description="Tokens per LLM call, by token type (input/output) and model",
        )
        self.llm_op_duration = meter.create_histogram(
            "gen_ai.client.operation.duration", unit="s",
            description="Duration of a single LLM call, by model",
        )
        self.llm_ttft = meter.create_histogram(
            "gen_ai.server.time_to_first_token", unit="s",
            description="Time to first streamed token, by model (streaming only)",
        )
        self.llm_finish = meter.create_counter(
            "gen_ai.finish_reasons", unit="1",
            description="LLM stop reasons by type (e.g. stop, length=truncated)",
        )
        self.llm_cost = meter.create_counter(
            "llm.cost.usd",  # no unit= : avoids a duplicate "_usd" suffix
            description="Estimated LLM spend in USD",
        )
        # --- evaluation metrics (only the bug-detector records these) ---
        self.eval_recall = meter.create_histogram(
            "eval.bug_recall", unit="1",
            description="Fraction of planted bugs the detector caught (0..1)",
        )
        self.eval_judge = meter.create_histogram(
            "eval.judge_quality", unit="1",
            description="LLM-as-judge quality score of the bug report (0..1)",
        )


# ---------------------------------------------------------------------------
# Identities
# ---------------------------------------------------------------------------
class Platform:
    """The orchestrator's telemetry identity: run-level metrics + a tracer.

    Owns no Langfuse project — the orchestrator coordinates but makes no LLM call.
    """

    def __init__(self, tracer, instruments, providers):
        self.tracer = tracer
        self.metrics = instruments
        self._providers = providers
        self._shut = False

    def shutdown(self):
        if self._shut:
            return
        self._shut = True
        for p in self._providers:
            p.shutdown()


class Agent:
    """One agent owner's telemetry identity: its own OTel providers (metrics +
    spans), its own instruments, and its own Langfuse client -> its own project.

    `run_trace()` opens ONE named, session-correlated trace for the owner across
    both streams; the agent's work runs inside it.
    """

    def __init__(self, agent_id, tracer, instruments, langfuse, keys, providers):
        self.agent_id = agent_id
        self.tracer = tracer
        self.metrics = instruments
        self.lf = langfuse
        self.keys = keys                       # (public, secret) for the Scores API
        self.base_labels = {"agent.id": agent_id}
        self._providers = providers
        self._last_trace_id = None
        self._otel_ctx = None       # stream-1 parent context for nested OTel spans
        self._shut = False

    @contextmanager
    def run_trace(self, session_id: str, name: str = None):
        """Open this owner's per-session trace on BOTH streams.

        Stream 1: an OTel span (nests under orchestrator.run -> one Tempo trace).
        Stream 2: an INDEPENDENT, named Langfuse trace in this owner's project,
                  stamped with session.id (the cross-owner correlation key). We pass
                  an explicit fresh trace_context so the Langfuse trace is the agent's
                  own — not a child of the orchestrator's OTel trace.
        """
        name = name or f"agent.{self.agent_id}"
        with self.tracer.start_as_current_span(name) as otspan:
            otspan.set_attribute("agent.id", self.agent_id)
            otspan.set_attribute("session.id", session_id)
            # Capture this OTel span as the stream-1 parent. The Langfuse root we
            # open next becomes the innermost *active* context (its own trace id),
            # so without this the LLM `chat` spans would inherit the Langfuse trace
            # id instead of nesting under orchestrator.run in Tempo. llm.generate
            # starts the chat span with THIS context to keep stream-1 nesting intact.
            self._otel_ctx = otel_trace.set_span_in_context(otspan)
            trace_context = {"trace_id": self.lf.create_trace_id()}
            self._last_trace_id = trace_context["trace_id"]
            with propagate_attributes(
                session_id=session_id, environment=ENV,
                trace_name=name, metadata={"agent.id": self.agent_id},
            ):
                with self.lf.start_as_current_observation(
                    trace_context=trace_context, as_type="span", name=name,
                ) as root:
                    yield root

    @property
    def last_trace_id(self):
        return self._last_trace_id

    def shutdown(self):
        if self._shut:
            return
        self._shut = True
        self.lf.flush()
        for p in self._providers:
            p.shutdown()


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
def setup_platform() -> Platform:
    """Build the orchestrator's identity. Call once at startup."""
    tracer_provider, meter_provider = _providers_for(PLATFORM_SERVICE)
    tracer = tracer_provider.get_tracer(f"{PLATFORM_SERVICE}.tracer")
    instruments = PlatformInstruments(meter_provider.get_meter(f"{PLATFORM_SERVICE}.meter"))
    platform = Platform(tracer, instruments, [tracer_provider, meter_provider])
    atexit.register(platform.shutdown)
    return platform


def setup_agent(agent_id: str, public_key: str, secret_key: str,
                host: str = None) -> Agent:
    """Build one agent owner's identity: OTel providers + Langfuse client -> project.

    `agent_id` is used verbatim as service.name AND agent.id, so the owner is one
    consistent identity across metrics, spans and its Langfuse project.
    """
    host = host or os.getenv("LANGFUSE_HOST", "http://localhost:3000")
    tracer_provider, meter_provider = _providers_for(agent_id, agent_id=agent_id)
    tracer = tracer_provider.get_tracer(f"{agent_id}.tracer")
    instruments = AgentInstruments(meter_provider.get_meter(f"{agent_id}.meter"))

    # This owner's Langfuse client -> this owner's project. Blocking all our OTel
    # tracer scopes keeps stream 1 out of Langfuse (stream separation).
    langfuse = Langfuse(
        public_key=public_key, secret_key=secret_key, host=host,
        blocked_instrumentation_scopes=_BLOCKED_SCOPES,
    )

    agent = Agent(agent_id, tracer, instruments, langfuse,
                  (public_key, secret_key), [tracer_provider, meter_provider])
    atexit.register(agent.shutdown)
    return agent
