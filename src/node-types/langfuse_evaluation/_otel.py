"""The trace of one experiment item, as OTLP/JSON for ``POST /api/public/otel/v1/traces``.

Langfuse links a trace to an experiment (dataset run) through span attributes; the older
``POST /dataset-run-items`` is deprecated. This module reproduces what the Langfuse Python
SDK sends for ``run_experiment``, captured from SDK 5.2.0 against a recording server:

* a root span ``experiment-item-run`` with a child ``experiment-item-task``; the task span
  is the item's root observation and carries the scores;
* the experiment id derived from project, dataset and run name, so every item of a run and
  every re-run under the same name land in the same experiment;
* metadata values JSON-encoded per key.

The scope is declared as that SDK version because the server decodes attribute values by
client version. When Langfuse changes the format, capture a newer SDK and update both the
constants and the attributes together.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime
from typing import Any

WIRE_SDK_NAME = "python"
WIRE_SDK_VERSION = "5.2.0"
SCOPE_NAME = "langfuse-sdk"
INGESTION_VERSION = "4"
ROOT_SPAN_NAME = "experiment-item-run"
TASK_SPAN_NAME = "experiment-item-task"
DEFAULT_ENVIRONMENT = "sdk-experiment"


def experiment_id(project_id: str, dataset_id: str, run_name: str) -> str:
    """The experiment id Langfuse itself derives for (project, dataset, run name)."""
    payload = json.dumps(
        ["langfuse-experiment-v1", project_id, dataset_id, run_name], separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def new_trace_id() -> str:
    return secrets.token_hex(16)


def new_span_id() -> str:
    return secrets.token_hex(8)


def serialize(value: Any) -> str | None:
    """Input and output: text stays text, anything else becomes JSON."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def serialize_metadata_value(value: Any) -> str:
    """One metadata value, JSON-encoded so that "123" and 123 stay distinct."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _attributes(values: dict[str, Any]) -> list[dict[str, Any]]:
    attributes = []
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            attributes.append({"key": key, "value": {"boolValue": value}})
        else:
            attributes.append({"key": key, "value": {"stringValue": str(value)}})
    return attributes


def _nanos(moment: datetime) -> str:
    moment = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    seconds = int(moment.timestamp())
    return str(seconds * 1_000_000_000 + moment.microsecond * 1000)


def _metadata(prefix: str, metadata: dict[str, Any]) -> dict[str, str]:
    return {f"{prefix}.{key}": serialize_metadata_value(value) for key, value in metadata.items() if value is not None}


def experiment_item_trace(
    *,
    public_key: str,
    trace_id: str,
    root_span_id: str,
    task_span_id: str,
    trace_name: str | None,
    experiment: str,
    run_name: str,
    run_description: str | None,
    run_metadata: dict[str, Any],
    dataset_id: str,
    item_id: str,
    item_input: Any,
    item_expected_output: Any,
    item_metadata: dict[str, Any],
    item_version: str | None,
    output: Any,
    error_message: str | None,
    environment: str,
    started_at: datetime,
    ended_at: datetime,
) -> dict[str, Any]:
    """The OTLP/JSON body with the two spans of one experiment item."""
    experiment_attributes = {
        "langfuse.experiment.id": experiment,
        "langfuse.experiment.name": run_name,
        "langfuse.experiment.metadata": serialize(run_metadata) if run_metadata else None,
        "langfuse.experiment.dataset.id": dataset_id,
        "langfuse.experiment.item.id": item_id,
        "langfuse.experiment.item.metadata": serialize(item_metadata) if item_metadata else None,
        "langfuse.experiment.item.root_observation_id": task_span_id,
    }
    item_attributes = {
        "langfuse.environment": environment,
        "langfuse.experiment.description": run_description,
        "langfuse.experiment.item.expected_output": serialize(item_expected_output),
        "langfuse.experiment.item.version": item_version,
    }
    observation_metadata = _metadata(
        "langfuse.observation.metadata",
        {
            **item_metadata,
            **run_metadata,
            "experiment_run_name": run_name,
            "dataset_id": dataset_id,
            "dataset_item_id": item_id,
        },
    )
    shown_output = f"Error: {error_message}" if error_message else serialize(output)
    failure = (
        {"langfuse.observation.level": "ERROR", "langfuse.observation.status_message": error_message}
        if error_message
        else {}
    )
    shared = {
        "langfuse.trace.name": trace_name,
        **item_attributes,
        **experiment_attributes,
        "langfuse.observation.type": "span",
        "langfuse.observation.input": serialize(item_input),
        "langfuse.observation.output": shown_output,
        **observation_metadata,
        **failure,
    }
    times = {"startTimeUnixNano": _nanos(started_at), "endTimeUnixNano": _nanos(ended_at)}
    root = {
        "traceId": trace_id,
        "spanId": root_span_id,
        "name": ROOT_SPAN_NAME,
        "kind": 1,
        **times,
        "attributes": _attributes({"langfuse.internal.is_app_root": True, **shared}),
        "status": {},
        "flags": 256,
    }
    task = {
        "traceId": trace_id,
        "spanId": task_span_id,
        "parentSpanId": root_span_id,
        "name": TASK_SPAN_NAME,
        "kind": 1,
        **times,
        "attributes": _attributes(shared),
        "status": {},
        "flags": 256,
    }
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": _attributes({"service.name": "limescape-plugin-langfuse"})},
                "scopeSpans": [
                    {
                        "scope": {
                            "name": SCOPE_NAME,
                            "version": WIRE_SDK_VERSION,
                            "attributes": _attributes({"public_key": public_key}),
                        },
                        "spans": [task, root],
                    }
                ],
            }
        ]
    }


def trace_headers(public_key: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "x-langfuse-sdk-name": WIRE_SDK_NAME,
        "x-langfuse-sdk-version": WIRE_SDK_VERSION,
        "x-langfuse-public-key": public_key,
        "x-langfuse-ingestion-version": INGESTION_VERSION,
    }
