"""OpenTelemetry tracing to Azure Monitor / Application Insights.

When APPLICATIONINSIGHTS_CONNECTION_STRING is set, Agent Framework spans (agent
runs, model calls, tool calls, workflow supersteps) and our own per-moment spans
are exported to Application Insights. Connect that resource to the Foundry
project (Foundry portal > Tracing) to inspect every agent run there.
Prompts and completions are only recorded when INSIGHTS_TRACE_CONTENT=true.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger(__name__)
_enabled = False


def setup_telemetry(service_name: str) -> bool:
    global _enabled
    if _enabled:
        return True
    from .agents import _load_dotenv

    _load_dotenv()
    conn = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not conn:
        return False
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
        from opentelemetry.sdk.resources import Resource

        # logger_name limits log export to our own loggers; Azure SDK HTTP chatter stays local.
        configure_azure_monitor(connection_string=conn, logger_name="insights",
                                resource=Resource.create({"service.name": service_name}))
        from agent_framework.observability import enable_instrumentation

        enable_instrumentation(enable_sensitive_data=os.getenv("INSIGHTS_TRACE_CONTENT", "false").lower() == "true")
        _enabled = True
        log.info("telemetry enabled for %s", service_name)
    except Exception as exc:  # noqa: BLE001 - telemetry must never break the broadcast
        log.warning("telemetry disabled: %s", exc)
    return _enabled


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """A tracing span that is a no-op when OpenTelemetry is not installed or configured."""
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover
        yield None
        return
    with trace.get_tracer("insights").start_as_current_span(
            name, attributes={k: v for k, v in attributes.items() if v is not None}) as s:
        yield s
