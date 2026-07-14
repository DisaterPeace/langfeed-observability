# Telemetry Contract

> ⚠️ **SUPERSEDED — historical Phase-0 planning sketch.** This described the earliest
> single-stream design (one OTel stream fanned out by the Collector to Langfuse + Prometheus,
> Haiku model, `agent.name`, one shared Langfuse trace per run). The system has since moved to
> a **two-stream, multi-owner** design. See **[observability-contract.md](observability-contract.md)**
> (the normative contract for agent owners) and **[report.md](report.md)** for the current shape.
> Kept only for history.

The exact shape of the telemetry every agent run emits. Agreed before coding so
that Langfuse traces and Grafana dashboards are clean by construction, not
retrofitted. This is the reference both the code and the report build on.

## Architecture recap

One OpenTelemetry instrumentation in the Python app; the OTel Collector fans it out:

```
agents (OTel SDK) ──► OTel Collector ──┬─► Langfuse      (traces  = LLM observability + evals)
                                       └─► Prometheus ──► Grafana (metrics = health dashboards)
```

The app has no knowledge of Langfuse/Prometheus — routing is the Collector's job.

## 1. Span tree (one trace per run)

```
orchestrator.run                    ROOT span = the whole trace
├── agent.repo_analyzer             the analyzer's entire turn
│   └── llm.generate                Haiku call        [Langfuse "generation"]
├── agent.bug_detector              the detector's entire turn
│   └── llm.generate                Haiku call        [Langfuse "generation"]
```

Three span types: the run root, one span per agent, and the LLM call.

## 2. Attributes per span

### orchestrator.run (root)
| attribute       | example        | purpose                         |
|-----------------|----------------|---------------------------------|
| `run.id`        | `run-a1b2`     | correlate across systems        |
| `run.input`     | sample repo    | what was asked                  |
| `run.outcome`   | `success`      | headline result (success/failure) |

### agent.repo_analyzer / agent.bug_detector
| attribute       | example         | purpose                        |
|-----------------|-----------------|--------------------------------|
| `agent.name`    | `repo_analyzer` | which agent (Grafana groups by this) |
| `agent.input`   | short summary   | what the agent was asked       |
| `agent.output`  | short summary   | what it produced               |

### llm.generate  (must render as a Langfuse "generation")
Uses **OpenTelemetry GenAI semantic conventions** so Langfuse auto-recognizes the
call, shows the conversation, and **computes cost from model + token counts**.

| attribute (GenAI convention)   | example            | purpose                     |
|--------------------------------|--------------------|-----------------------------|
| `gen_ai.system`                | `anthropic`        | provider                    |
| `gen_ai.request.model`         | `claude-haiku-4-5` | model → drives cost calc    |
| `gen_ai.request.max_tokens`    | `1024`             | request param               |
| `gen_ai.usage.input_tokens`    | `812`              | prompt tokens               |
| `gen_ai.usage.output_tokens`   | `143`              | completion tokens           |
| input messages / output text   | the conversation   | Langfuse shows prompt+reply |

Rationale: standard `gen_ai.*` names are what Langfuse's OTel mapping looks for.
Off-standard names would render as a plain span with no cost. This is the concrete
OTel↔Langfuse integration point the report highlights.

## 3. Metrics (→ Prometheus → Grafana)

Emitted from the same instrumentation as the traces.

| metric                        | type                | Grafana panel        |
|-------------------------------|---------------------|----------------------|
| `gen_ai.client.token.usage`   | counter (type,model)| tokens over time     |
| `llm.cost.usd`                | counter             | spend trend          |
| `run.duration`                | histogram           | run latency p95      |
| `runs.total` (by outcome)     | counter             | success rate         |
| `agent.duration` (by agent)   | histogram           | per-agent latency    |

## 4. Error handling

Each layer wraps its work in try/except. On failure it sets its OWN span status to
`Error` and records the exception. Errors do NOT auto-propagate to the parent, so
the orchestrator explicitly marks its root span failed too — yielding one red trace
top to bottom. `runs.total{outcome="failure"}` also increments.

## 5. The workload (what the agents actually do)

- **Target:** a small planted sample repo under `sample_repo/` with a couple of
  deliberate bugs.
- **repo_analyzer:** reads the sample repo, produces a short structural summary.
- **bug_detector:** consumes that summary + the code, reports likely bugs.
- **orchestrator:** runs analyzer then detector, passing context between them.
