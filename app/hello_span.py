r"""
Phase 0a verification — emit ONE trace by hand and ship it to the Collector.

Run (from D:\agent-service):
    .venv\Scripts\python app\hello_span.py

Then watch it arrive:
    docker compose logs --tail 40 otel-collector
  (the debug exporter prints the full span record right there)

------------------------------------------------------------------------------
THE FOUR OTel SDK OBJECTS 
------------------------------------------------------------------------------
  Resource        : WHO is emitting. Key/value metadata (service.name, version)
                    attached to EVERY span. This is how Tempo groups traces by
                    service. Get service.name wrong and your traces vanish into
                    "unknown_service".
  TracerProvider  : the FACTORY. Owns the resource + the export pipeline.
                    You create exactly one per process.
  SpanProcessor   : the PLUMBING. Decides HOW spans leave — batched in the
                    background (BatchSpanProcessor, for real use) or one-at-a-
                    time synchronously (SimpleSpanProcessor, handy for demos).
  SpanExporter    : the DESTINATION. Here: OTLP over HTTP to the Collector.
------------------------------------------------------------------------------
"""

from dotenv import load_dotenv
load_dotenv()  # pulls OTEL_EXPORTER_OTLP_HEADERS (bearer token) from .env

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.trace import Status, StatusCode

# 1) WHO am I? This metadata rides along on every span.
resource = Resource.create({
    "service.name": "hello-agent-service",   # <- how you'll find it later in Langfuse
    "service.version": "0.0.1",
})

# 2) The factory, bound to that identity.
provider = TracerProvider(resource=resource)

# 3) The exporter: OTLP/HTTP to the Collector's 4318 receiver.
#    The "/v1/traces" path is the OTLP HTTP convention; the SDK appends it if
#    you give the base endpoint, but we spell it out to be explicit/teachable.
exporter = OTLPSpanExporter(endpoint="http://localhost:4318/v1/traces")

# 4) Wire exporter -> provider via a processor.
#    SimpleSpanProcessor = export immediately, synchronously. Perfect for a demo
#    because the span is gone the instant the `with` block closes — no waiting.
provider.add_span_processor(SimpleSpanProcessor(exporter))

# Register our provider as THE global one, then grab a tracer from it.
trace.set_tracer_provider(provider)
tracer = trace.get_tracer("hello-agent-service.tracer")

# ---- Emit a tiny trace: one parent span with a nested child span ----------
# A span = one timed unit of work. Nesting them builds the trace tree that
# later becomes the orchestrator -> agent -> llm-call hierarchy.
with tracer.start_as_current_span("orchestrator.demo") as parent:
    parent.set_attribute("demo.kind", "phase-0a-verification")
    parent.set_attribute("run.id", "manual-001")

    with tracer.start_as_current_span("agent.fake_work") as child:
        child.set_attribute("agent.name", "placeholder")
        child.set_attribute("tokens.total", 123)        # pretend metric
        # (real work would happen here)
        child.set_attribute("model.name", "claude-haiku-4-5")
        child.set_status(Status(StatusCode.ERROR, "pretend the bug-detector crashed"))

print("Span emitted. Run: docker compose logs --tail 40 otel-collector")
