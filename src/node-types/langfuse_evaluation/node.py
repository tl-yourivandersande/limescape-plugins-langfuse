"""Run evaluations against a Langfuse dataset from a flow.

One node, five operations: read the items of a dataset, read one item, step through a
list of items in a loop, record the result of one item as an experiment trace with its
scores, and write scores on the experiment as a whole.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from limescape_plugin_sdk import NodeContext, NodeInput, NodeInputError

from . import _otel
from ._client import Langfuse, segment

OPERATIONS = ("dataset_items", "dataset_item", "next_item", "experiment_item", "experiment_scores")
SCORE_DATA_TYPES = ("NUMERIC", "BOOLEAN", "CATEGORICAL", "TEXT")
SCORE_CONFIG_MODES = ("link", "require", "ignore")
STATE_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_SCORES = 200
# Fields a run can set through the flow input: a non-empty value under the same key in the
# flow state wins from the value configured on the node. A missing {{ variable }} fails a
# run, so this is how an optional selection reaches the node.
INPUT_OVERRIDES = frozenset({"dataset_name", "dataset_version", "metadata_filter", "run_name", "run_description"})


# --- reading parameters -------------------------------------------------------------


def _value(inp: NodeInput, name: str) -> Any:
    if name in INPUT_OVERRIDES:
        override = inp.input_data.get(name)
        if override not in (None, "", [], {}):
            return override
    return inp.param(name)


def _text(inp: NodeInput, name: str, default: str = "") -> str:
    value = _value(inp, name)
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        raise NodeInputError(f"{name} must be text")
    return str(value).strip() or default


def _required_text(inp: NodeInput, name: str) -> str:
    value = _text(inp, name)
    if not value:
        raise NodeInputError(f"{name} is required")
    return value


def _structured(inp: NodeInput, name: str) -> Any:
    """A JSON field: text is parsed, a value from a {{ variable }} is used as it is."""
    value = _value(inp, name)
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            return json.loads(value)
        except ValueError:
            raise NodeInputError(f"{name} is not valid JSON") from None
    return value


def _flag(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "ja", "on"}


def _timestamp(value: str, name: str) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise NodeInputError(f"{name} must be an ISO date-time, for example 2026-10-27T07:52:00Z") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(UTC)


# --- dataset items ------------------------------------------------------------------


def _item(row: dict[str, Any], *, content: bool) -> dict[str, Any]:
    item = {
        "id": row.get("id"),
        "dataset_id": row.get("datasetId"),
        "dataset_name": row.get("datasetName"),
        "metadata": row.get("metadata") if isinstance(row.get("metadata"), dict) else {},
    }
    if content:
        item["input"] = row.get("input")
        item["expected_output"] = row.get("expectedOutput")
    return item


def _matches(metadata: dict[str, Any], wanted: dict[str, Any]) -> bool:
    """Every filter key equals the item's metadata value, or is one of the listed values."""
    for key, value in wanted.items():
        allowed = value if isinstance(value, list) else [value]
        allowed = [str(v) for v in allowed if v not in (None, "")]
        if allowed and str(metadata.get(key)) not in allowed:
            return False
    return True


async def _dataset_items(ctx: NodeContext, inp: NodeInput, langfuse: Langfuse) -> dict[str, Any]:
    name = _required_text(inp, "dataset_name")
    version = _timestamp(_text(inp, "dataset_version"), "dataset_version")
    wanted = _structured(inp, "metadata_filter") or {}
    if not isinstance(wanted, dict):
        raise NodeInputError("metadata_filter must be a JSON object, for example {\"split\": \"test\"}")
    rows = await langfuse.pages(
        "dataset-items", {"datasetName": name, "version": _iso(version) if version else None}
    )
    content = _flag(inp.param("include_content"), True)
    items = [
        _item(row, content=content)
        for row in sorted(rows, key=lambda row: str(row.get("id")))
        if row.get("status", "ACTIVE") == "ACTIVE" and _matches(row.get("metadata") or {}, wanted)
    ]
    if not items:
        raise NodeInputError(f"Dataset '{name}' has no active items that match the selection")
    started = _now()
    run_name = _text(inp, "run_name") or f"{name}-{started:%Y%m%d-%H%M}"
    ctx.trace.event("langfuse_evaluation", {"operation": "dataset_items", "items": len(items), "of": len(rows)})
    return {
        "dataset_name": name,
        "dataset_id": items[0]["dataset_id"],
        # The version the items were read at; pass it on so a run stays on one version.
        "dataset_version": _iso(version or started),
        "run_name": run_name,
        "count": len(items),
        "items": items,
    }


async def _dataset_item(ctx: NodeContext, inp: NodeInput, langfuse: Langfuse) -> dict[str, Any]:
    item_id = segment(_text(inp, "item_id"), "item_id")
    row = await langfuse.request("GET", f"dataset-items/{item_id}")
    if not isinstance(row, dict) or not row.get("id"):
        raise NodeInputError("Langfuse returned an unexpected answer for this dataset item")
    item = {**_item(row, content=True), "fetched_at": _iso(_now())}
    result = {inp.output_key: item}
    prefix = _text(inp, "state_key_prefix")
    if prefix:
        # Copies of the input fields as their own flow-state keys, for an agent call that
        # takes its input from named keys.
        if not isinstance(item["input"], dict):
            raise NodeInputError("state_key_prefix needs a dataset item whose input is a JSON object")
        for key, value in item["input"].items():
            state_key = f"{prefix}{key}"
            if not STATE_KEY.match(state_key) or state_key in (inp.output_key, "error"):
                raise NodeInputError(f"'{state_key}' cannot be used as a flow-state key")
            result[state_key] = value
    ctx.trace.event("langfuse_evaluation", {"operation": "dataset_item", "item": item["id"]})
    return result


def _next_item(inp: NodeInput) -> dict[str, Any]:
    """Item number <iteration> of a list, or ``done`` once the list is used up. No network."""
    items = _structured(inp, "items")
    if isinstance(items, dict):
        items = items.get("items")
    if not isinstance(items, list):
        raise NodeInputError("items must be a list, for example {{ dataset_ophalen.items }}")
    index = inp.iteration
    if index is None:
        # Outside a platform loop counter: one further than what this node produced so far.
        earlier = inp.input_data.get(inp.output_key)
        index = 1 + (len(earlier) if isinstance(earlier, list) else 1 if earlier else 0)
    total = len(items)
    if index > total:
        return {"done": True, "index": index, "total": total, "item": None, "item_id": None}
    item = items[index - 1]
    item_id = item.get("id") if isinstance(item, dict) else item
    return {"done": False, "index": index, "total": total, "item": item, "item_id": item_id}


# --- scores -------------------------------------------------------------------------


def _scores(raw: Any) -> list[dict[str, Any]]:
    """Validate the scores of the flow: [{name, value, data_type?, comment?, metadata?}]."""
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = raw.get("scores", [raw] if "name" in raw else None)
    if not isinstance(raw, list):
        raise NodeInputError("scores must be a list of {name, value, data_type, comment}")
    if len(raw) > MAX_SCORES:
        raise NodeInputError(f"At most {MAX_SCORES} scores per call")
    scores = []
    for entry in raw:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not entry["name"].strip():
            raise NodeInputError("Every score needs a name")
        name, value = entry["name"].strip(), entry.get("value")
        data_type = entry.get("data_type") or entry.get("dataType")
        if isinstance(value, bool):
            value, data_type = float(value), data_type or "BOOLEAN"
        elif isinstance(value, (int, float)):
            value = float(value)
        elif not isinstance(value, str) or not value:
            raise NodeInputError(f"Score '{name}' has no value")
        if data_type is not None and data_type not in SCORE_DATA_TYPES:
            raise NodeInputError(f"Score '{name}' has an unknown data type; use one of {', '.join(SCORE_DATA_TYPES)}")
        comment = entry.get("comment")
        scores.append(
            {
                "name": name,
                "value": value,
                "dataType": data_type,
                "comment": str(comment) if comment not in (None, "") else None,
                "metadata": entry.get("metadata") if isinstance(entry.get("metadata"), dict) else None,
                "configId": entry.get("config_id") or entry.get("configId"),
            }
        )
    return scores


async def _link_score_configs(langfuse: Langfuse, scores: list[dict[str, Any]], mode: str) -> None:
    """Attach the score config with the same name, so Langfuse shows and checks scores consistently."""
    if mode not in SCORE_CONFIG_MODES:
        raise NodeInputError(f"score_configs must be one of {', '.join(SCORE_CONFIG_MODES)}")
    if mode == "ignore" or not scores:
        return
    configs = {
        config["name"]: config["id"]
        for config in await langfuse.pages("score-configs")
        if config.get("name") and config.get("id") and not config.get("isArchived")
    }
    missing = sorted({score["name"] for score in scores if not score["configId"] and score["name"] not in configs})
    if missing and mode == "require":
        raise NodeInputError("Langfuse has no score config for: " + ", ".join(missing))
    for score in scores:
        score["configId"] = score["configId"] or configs.get(score["name"])


def _score_id(*parts: str) -> str:
    """A stable id per target and score name: running a node again updates instead of duplicates."""
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


async def _post_scores(langfuse: Langfuse, bodies: list[dict[str, Any]]) -> None:
    if not bodies:
        return
    result = await langfuse.request(
        "POST",
        "scores",
        body=[{key: value for key, value in body.items() if value is not None} for body in bodies],
    )
    rejected = result.get("rejected") if isinstance(result, dict) else None
    if rejected:
        raise NodeInputError(f"Langfuse rejected {rejected} of {len(bodies)} scores; check names, values and configs")


# --- experiments --------------------------------------------------------------------


def _run_metadata(inp: NodeInput) -> dict[str, Any]:
    metadata = _structured(inp, "run_metadata") or {}
    if not isinstance(metadata, dict):
        raise NodeInputError("run_metadata must be a JSON object")
    return metadata


async def _experiment_item(ctx: NodeContext, inp: NodeInput, langfuse: Langfuse) -> dict[str, Any]:
    run_name = _required_text(inp, "run_name")
    item = _structured(inp, "item")
    if not isinstance(item, dict) or not item.get("id") or not item.get("dataset_id"):
        raise NodeInputError("item must be a dataset item as this node returns it, with id and dataset_id")
    error_message = _text(inp, "error_message") or None
    output = _value(inp, "output")
    if isinstance(output, str) and output.strip()[:1] in ("{", "["):
        try:
            output = json.loads(output)
        except ValueError:
            pass  # text that only looks like JSON stays text
    if output in (None, "") and not error_message:
        raise NodeInputError("output is required, or give error_message when the task failed")
    scores = _scores(_structured(inp, "scores"))
    await _link_score_configs(langfuse, scores, _text(inp, "score_configs", "link"))

    ended = _now()
    started = _timestamp(_text(inp, "started_at"), "started_at") or ended
    version = _timestamp(_text(inp, "dataset_version"), "dataset_version")
    project_id = await langfuse.project_id()
    experiment = _otel.experiment_id(project_id, str(item["dataset_id"]), run_name)
    trace_id, root_span_id, task_span_id = _otel.new_trace_id(), _otel.new_span_id(), _otel.new_span_id()
    await langfuse.request(
        "POST",
        "otel/v1/traces",
        headers=_otel.trace_headers(langfuse.public_key),
        body=_otel.experiment_item_trace(
            public_key=langfuse.public_key,
            trace_id=trace_id,
            root_span_id=root_span_id,
            task_span_id=task_span_id,
            trace_name=_text(inp, "trace_name") or None,
            experiment=experiment,
            run_name=run_name,
            run_description=_text(inp, "run_description") or None,
            run_metadata=_run_metadata(inp),
            dataset_id=str(item["dataset_id"]),
            item_id=str(item["id"]),
            item_input=item.get("input"),
            item_expected_output=item.get("expected_output"),
            item_metadata=item.get("metadata") if isinstance(item.get("metadata"), dict) else {},
            item_version=_iso(version) if version else None,
            output=output,
            error_message=error_message,
            environment=_text(inp, "environment", _otel.DEFAULT_ENVIRONMENT),
            started_at=min(started, ended),
            ended_at=ended,
        ),
    )
    timestamp = _iso(ended)
    await _post_scores(
        langfuse,
        [
            {
                "id": _score_id(trace_id, score["name"]),
                "traceId": trace_id,
                "observationId": task_span_id,
                "timestamp": timestamp,
                **score,
            }
            for score in scores
        ],
    )
    ctx.trace.event(
        "langfuse_evaluation",
        {"operation": "experiment_item", "run": run_name, "item": item["id"], "scores": len(scores)},
    )
    return {
        "run_name": run_name,
        "experiment_id": experiment,
        "dataset_item_id": item["id"],
        "trace_id": trace_id,
        "observation_id": task_span_id,
        "scores_written": len(scores),
        "trace_url": f"{langfuse.base_url}/project/{project_id}/traces/{trace_id}",
    }


async def _experiment_scores(ctx: NodeContext, inp: NodeInput, langfuse: Langfuse) -> dict[str, Any]:
    scores = _scores(_structured(inp, "scores"))
    if not scores:
        raise NodeInputError("scores is required")
    experiment = _text(inp, "experiment_id")
    project_id = await langfuse.project_id()
    if not experiment:
        # Without an id from an earlier step: the same derivation, from dataset and run name.
        dataset_name, run_name = _required_text(inp, "dataset_name"), _required_text(inp, "run_name")
        dataset = await langfuse.request("GET", f"v2/datasets/{segment(dataset_name, 'dataset_name')}")
        if not isinstance(dataset, dict) or not dataset.get("id"):
            raise NodeInputError(f"Langfuse returned an unexpected answer for dataset '{dataset_name}'")
        experiment = _otel.experiment_id(project_id, str(dataset["id"]), run_name)
    await _link_score_configs(langfuse, scores, _text(inp, "score_configs", "link"))
    timestamp = _iso(_now())
    await _post_scores(
        langfuse,
        [
            {"id": _score_id(experiment, score["name"]), "datasetRunId": experiment, "timestamp": timestamp, **score}
            for score in scores
        ],
    )
    ctx.trace.event(
        "langfuse_evaluation", {"operation": "experiment_scores", "experiment": experiment, "scores": len(scores)}
    )
    return {
        "experiment_id": experiment,
        "scores_written": len(scores),
        "experiment_url": f"{langfuse.base_url}/project/{project_id}/experiments/results?baseline={experiment}",
    }


async def execute(ctx: NodeContext, inp: NodeInput):
    """Errors are raised, so the node fails and the flow stops instead of scoring half a run."""
    operation = _text(inp, "operation", "dataset_items")
    if operation not in OPERATIONS:
        raise NodeInputError(f"Unknown operation '{operation}'")
    if operation == "next_item":
        yield {inp.output_key: _next_item(inp)}
        return
    langfuse = await Langfuse.connect(ctx, inp.param("credential_code"))
    if operation == "dataset_items":
        yield {inp.output_key: await _dataset_items(ctx, inp, langfuse)}
    elif operation == "dataset_item":
        yield await _dataset_item(ctx, inp, langfuse)
    elif operation == "experiment_item":
        yield {inp.output_key: await _experiment_item(ctx, inp, langfuse)}
    else:
        yield {inp.output_key: await _experiment_scores(ctx, inp, langfuse)}
