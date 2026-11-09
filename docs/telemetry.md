# OpenTelemetry traces

Chainlit can export one OpenTelemetry trace per chat turn to an OTLP/HTTP
collector. Each turn is a root span; each `Step` that runs inside it becomes a
child span.

Telemetry is off by default. It is opt-in and needs the optional `otel` extra.
The base install does not import the OpenTelemetry SDK or exporter.

## Enable telemetry

Install the extra:

```bash
pip install "chainlit[otel]"
```

Enable it in `.chainlit/config.toml`:

```toml
[features.telemetry]
enabled = true
capture_names = false
sample_ratio = 0.1
```

`enabled` is the only enable switch; an OTLP environment variable does not
turn telemetry on by itself. Changes to this section need a process restart.

Two standard OTel environment variables force the disabled path even when the
config says enabled:

- `OTEL_SDK_DISABLED=true`
- `OTEL_TRACES_EXPORTER=none`

### Verify

After a restart the startup log shows one line such as:

```
telemetry: exporting traces to http://localhost:4318
```

Send one message, then look for a `chainlit.turn` span with
`chainlit.turn.kind=message` and its `chainlit.step.*` children in the
collector. No spans means the flag, the `otel` extra, or one of the two
override variables above is still off. An unreachable collector logs a bounded
warning and never changes the chat turn.

### Disable

Set `enabled = false`, or remove the section, and restart the process. Chainlit
then creates no provider and exports nothing. To keep the config and override
it for one run, start with `OTEL_SDK_DISABLED=true` or
`OTEL_TRACES_EXPORTER=none`. Uninstalling the `otel` extra also stops export,
but logs a one-time warning while the config still enables it.

## Export to a collector

The exporter is fixed to OTLP/HTTP/protobuf. The default endpoint is
`http://localhost:4318/v1/traces`.

Endpoint precedence:

1. `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, used as-is.
2. `OTEL_EXPORTER_OTLP_ENDPOINT`, with `/v1/traces` appended.
3. The default local endpoint.

The endpoint must be an `http` or `https` URL with a host. Chainlit rejects
URLs that contain credentials, a query string, or a fragment. Startup logs
only `scheme://host:port`; the path, query, and userinfo are never logged.

If the endpoint is plain `http` and the host is not loopback, Chainlit logs a
warning once but still starts. Put credentials in headers, not in the URL.

Header precedence:

1. `OTEL_EXPORTER_OTLP_TRACES_HEADERS`, when non-empty.
2. `OTEL_EXPORTER_OTLP_HEADERS`.

The value is a comma-separated list of `name=value` pairs. Malformed entries
are dropped. Startup logs the number of dropped entries and the validated
header names, never the header values. Header values are percent-decoded.

`OTEL_EXPORTER_OTLP_PROTOCOL` is not supported for gRPC: Chainlit logs one
warning and uses OTLP/HTTP.

The following standard variables are passed through to the SDK where they
apply: `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_BSP_*`,
`OTEL_TRACES_SAMPLER`, and `OTEL_TRACES_SAMPLER_ARG`. `OTEL_SERVICE_NAME`
defaults to `chainlit`. `OTEL_RESOURCE_ATTRIBUTES` is operator-controlled, so
treat it as another place where PII could be configured.

## Sampling

When `OTEL_TRACES_SAMPLER` is set, it wins, together with
`OTEL_TRACES_SAMPLER_ARG`. Chainlit does not override the standard sampler
environment.

When `OTEL_TRACES_SAMPLER` is unset, Chainlit uses
`ParentBased(TraceIdRatioBased(sample_ratio))`. The default `sample_ratio` is
`0.1`, so a busy app does not create an unobserved trace bill. The root span
makes the decision and child spans inherit it. `sample_ratio` is ignored when
`OTEL_TRACES_SAMPLER` is set.

Sampling only decides whether spans are recorded. A non-sampled turn still
gets a trace id and a turn root, so parent/child shape and the app context API
still work.

## Span names

By default, span names are low-cardinality and contain no app strings:

- `chainlit.turn` for a turn root.
- `chainlit.<step type>` for a step, such as `chainlit.tool` or
  `chainlit.llm`.

Turn roots use `chainlit.turn.kind` values `message`, `edit_message`,
`chat_start`, `chat_resume`, and `audio_end`.

With `capture_names = true`, a step span uses the step name as its span name
and attaches `chainlit.step.name`. Names are stripped of all Unicode `Cc` and
`Cf` characters, including bidi controls such as U+202E, and truncated to 128
characters. An empty sanitized name falls back to `chainlit.<step type>`.

**Privacy warning:** step names are app text. `@cl.step(name=...)` and
`Step(name=...)` can interpolate user input. `capture_names` is off by default;
turn it on only when names do not contain user data, or when the trace backend
is access-controlled for that data.

## Attributes and privacy

By default Chainlit exports only these attributes:

| Attribute                          | Span         | Value                                |
| ---------------------------------- | ------------ | ------------------------------------ |
| `chainlit.turn.kind`               | root         | fixed turn kind                      |
| `chainlit.thread.id`               | root         | session thread UUID                  |
| `chainlit.turn.cancelled`          | root         | boolean, only when cancelled         |
| `chainlit.telemetry.dropped_spans` | root         | count, only when the span cap is hit |
| `chainlit.step.type`               | step         | fixed step type                      |
| `chainlit.step.is_error`           | step         | boolean                              |
| `chainlit.step.name`               | step         | opt-in, only with `capture_names`    |
| `chainlit.span.unclosed`           | step         | boolean, only when cleanup closed it |
| `error.type`                       | root or step | sanitized exception class name       |

Chainlit does not attach message text, prompt or completion text, tool
arguments or results, element content, uploaded file names, user ids, session
ids, request headers, cookies, environment variables, hostnames, exception
messages, or stack traces.

`chainlit.thread.id` is re-validated as a canonical UUID when the span is
created. An invalid value is omitted without raising. The value is
**client-supplied correlation, not authorization**: thread ownership is
enforced by the connect authentication path and thread routes, never by
telemetry. Chainlit does not hash the id in this version.

Root spans carry no inbound parent. Chainlit does not read `traceparent`,
`tracestate`, or `baggage` from the browser or from message payloads, and does
not inject baggage. Do not treat a traced request as authenticated because it
has a trace context.

## Bounds and failure behavior

Each turn exports at most 200 step spans plus its root span. Steps after the
cap are not exported and are counted in `chainlit.telemetry.dropped_spans` on
the root, so a 1,000-step turn produces 200 step spans and
`dropped_spans=800`. The per-turn handle registry is released when the turn
ends.

Every exported span carries at most 8 attributes, and every string value is at
most 128 characters. Attribute values are scalars only: no dict, list, tuple,
or set. Chainlit adds no span events and no span links. Resource attributes
(`service.name`, `service.version`, and anything from
`OTEL_RESOURCE_ATTRIBUTES`) are attached to the resource, not the span, and are
excluded from the 8-attribute count. They are operator-controlled; bound them
at the collector if they can contain sensitive values.

Exporter failures, a full export queue, and a slow or unreachable collector are
isolated from the turn: the turn completes normally, and each failure class is
logged at most once per rate-limit window instead of once per span. Shutdown
flushes on a daemon thread for at most 2 seconds, so a stalled collector cannot
hold up process exit; spans still queued at that point are dropped.

## App spans and propagation

Use `chainlit.otel.get_tracer()` to create app spans that export through
Chainlit's tracer and join the current turn:

```python
from chainlit.otel import current_traceparent, get_tracer

tracer = get_tracer("my-app")

async with cl.Step(name="call downstream", type="tool"):
    with tracer.start_as_current_span("downstream.request"):
        traceparent = current_traceparent()
```

`get_tracer()` returns Chainlit's tracer when telemetry is enabled, even when
the app has installed its own global provider. It returns a no-op tracer when
telemetry is disabled. Spans created with it use Chainlit's instrumentation
scope and therefore export through Chainlit's exporter.

`current_traceparent()` returns the W3C `traceparent` string for the current
span, root or step, or `None` when telemetry is disabled or there is no valid
span. Use it when you must pass trace context across a process boundary
yourself. Chainlit does not propagate context to MCP or HTTP calls on your
behalf; use the app's standard OpenTelemetry instrumentation for outbound
calls.

Provider ownership: Chainlit creates its own tracer provider and only installs
it as the global provider when the global slot is still empty. If the app
already has a provider, Chainlit leaves it unchanged and keeps using its own.
If Chainlit started first, a later `trace.set_tracer_provider()` from the app
is rejected by OpenTelemetry. Configure the app provider before Chainlit
starts, or use `chainlit.otel.get_tracer()`.

## Retention and multi-tenant deployments

Chainlit does not store spans. The operator supplies the OTLP endpoint and is
responsible for trace-backend access control, retention, and PII handling.

One process can serve many users. Default attributes are pseudonymous, but
`chainlit.thread.id` and opt-in step names can be personal data when combined
with operator-side records. For multi-tenant deployments, keep
`capture_names` off or restrict trace-backend access. Chainlit does not send
telemetry to a vendor by default.
