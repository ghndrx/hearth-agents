"""OpenTelemetry span + structured-log scaffolder.

Research #3841 (autonomous observability instrumentation): the agent
should bake observability INTO new code during scaffold, not retrofit
after the fact. This tool emits a Go/Python/TypeScript snippet with
proper span start/end + structured-log wrapper + trace-context
propagation, which the agent pastes into new route handlers via
``edit_file``.

Non-invasive: emits text for the agent to paste, doesn't write files
itself. Mirrors scaffold_test_file's stub-skeleton pattern.
"""

from __future__ import annotations

from langchain_core.tools import tool


_GO = """\
// OTel instrumentation snippet for a new handler {function}.
// Paste into the top of the handler; replace the TODO with the real work.
//
// Required imports:
//   "go.opentelemetry.io/otel"
//   "go.opentelemetry.io/otel/attribute"

func (h *Handler) {function}(w http.ResponseWriter, r *http.Request) {{
    ctx, span := otel.Tracer("hearth/{service}").Start(r.Context(), "{function}")
    defer span.End()
    span.SetAttributes(
        attribute.String("http.route", "{route}"),
        attribute.String("http.method", r.Method),
    )
    // TODO: handler body. Pass ctx into downstream calls so the
    // trace context propagates.
    // On error: span.RecordError(err); span.SetStatus(codes.Error, err.Error())
    log.Info().
        Str("trace_id", span.SpanContext().TraceID().String()).
        Str("route", "{route}").
        Msg("{function}")
}}
"""

_PY = """\
# OTel instrumentation for {function} (paste into the route handler).
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
_tracer = trace.get_tracer("hearth.{service}")

async def {function}(request):
    with _tracer.start_as_current_span("{function}") as span:
        span.set_attribute("http.route", "{route}")
        span.set_attribute("http.method", request.method)
        try:
            # TODO: handler body
            return {{}}
        except Exception as e:
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)[:200]))
            raise
"""

_TS = """\
// OTel instrumentation for {function} (paste into the route handler).
import {{ trace, SpanStatusCode }} from '@opentelemetry/api';
const tracer = trace.getTracer('hearth.{service}');

export async function {function}(req, res) {{
  return tracer.startActiveSpan('{function}', async (span) => {{
    span.setAttribute('http.route', '{route}');
    span.setAttribute('http.method', req.method);
    try {{
      // TODO: handler body
      res.status(200).json({{}});
    }} catch (e) {{
      span.recordException(e);
      span.setStatus({{ code: SpanStatusCode.ERROR, message: String(e).slice(0, 200) }});
      throw e;
    }} finally {{
      span.end();
    }}
  }});
}}
"""


@tool
def scaffold_otel(
    language: str,
    service: str,
    function: str,
    route: str,
) -> str:
    """Return an OpenTelemetry span + structured-log instrumentation
    snippet for a new route handler. Paste the output into your
    handler via ``edit_file``; the trace context (ctx, span) is
    threaded so downstream DB/HTTP calls inherit it.

    Args:
        language: go | py | ts
        service: service name for the Tracer ("auth", "messaging", etc).
        function: handler function name.
        route: HTTP route the handler serves (for attribute logging).
    """
    lang = language.lower()
    if lang in ("go", "golang"):
        return _GO.format(function=function, service=service, route=route)
    if lang in ("py", "python"):
        return _PY.format(function=function, service=service, route=route)
    if lang in ("ts", "js", "typescript", "javascript"):
        return _TS.format(function=function, service=service, route=route)
    return f"error: unsupported language {language!r}; use go|py|ts"
