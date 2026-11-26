# Langfuse evaluation

One node for running evaluations against a Langfuse dataset. Pick the operation with **Operation**;
the form only shows the fields that operation uses. The result is stored under the node's output key.
A failed call fails the node, so the flow stops instead of recording half a run.

The node uses the Langfuse connection from the team settings (Team settings > Langfuse): base URL,
public key and secret key of one Langfuse project. The keys never appear in output or errors.

| Operation | Needs | Result |
|---|---|---|
| Get dataset items | Dataset name; optional version, metadata filter, run name | `{dataset_name, dataset_id, dataset_version, run_name, count, items: [{id, dataset_id, dataset_name, metadata, input, expected_output}]}` |
| Get one dataset item | Dataset item ID; optional prefix | `{id, dataset_id, dataset_name, metadata, input, expected_output, fetched_at}` |
| Next item of a list (loop) | Items | `{done, index, total, item, item_id}` |
| Record experiment result | Run name, dataset item, output; optional scores | `{run_name, experiment_id, dataset_item_id, trace_id, observation_id, scores_written, trace_url}` |
| Write scores on the experiment | Scores, and an experiment ID or dataset name + run name | `{experiment_id, scores_written, experiment_url}` |

## Reading a dataset

**Get dataset items** reads every page (at most 2000 items) and returns the active items, sorted by id.
The metadata filter keeps items whose metadata has the given values: `{"split": "test"}` or
`{"dossier": ["D01", "D03"]}`; empty values are ignored. Turn off **Include input and expected output**
to get only ids and metadata, and let a later step fetch each item with **Get one dataset item**.

The result carries the `run_name` for the rest of the run. `{timestamp}` in the run name is replaced by the
start of the run (UTC, `yyyymmdd-hhmmss`), so `dossier-analyse-{timestamp}` gives every run its own
experiment; an empty run name means `<dataset name>-{timestamp}`.

For this operation `dataset_name`, `dataset_version`, `metadata_filter` and `run_name` can also come from
the flow input: a non-empty value under the same key in the flow state wins from the value on the node.
That is how one flow serves both a full run and a run on a few items. The other operations do not look
at the flow input; give them the values from this result.

**Get one dataset item** can also put each field of the item input in the flow state as its own key.
With prefix `eval_` an input `{"zaak": ..., "documenten": ...}` becomes `eval_zaak` and `eval_documenten`,
which an agent call can take as its input keys.

**Next item of a list** is the loop head: each time the node runs it returns the next item of the list,
and `done: true` after the last one. Route on `done` to leave the loop. Give the node a maximum number of
iterations of at least the number of items plus one. It makes no request.

## Recording an experiment

**Record experiment result** writes one trace for one dataset item and links it to the experiment with
that run name; the scores are attached to it. All items recorded under the same run name form one
experiment, and recording under an existing run name adds to that experiment.

Scores are a list of `{name, value, data_type, comment, metadata}`. `true`/`false` becomes a boolean
score, a number a numeric score, text a categorical score. With **Score configs** on *link* a score gets
the Langfuse score config with the same name when one exists; *require* fails when one is missing.

Fill **Error message** when the solution failed on the item: the trace then has level ERROR and no
output. **Started at** sets the start of the trace, so Langfuse shows how long the item took.

**Write scores on the experiment** attaches scores to the run as a whole, such as the share of items
that passed every hard requirement.

## How the trace is sent

Langfuse links a trace to an experiment through span attributes on its OpenTelemetry endpoint. The node
sends what the Langfuse Python SDK (5.2.0) sends for `run_experiment`: a root span `experiment-item-run`
with a child `experiment-item-task` that carries input, output, expected output, item and run metadata
and the scores. The experiment id is derived from project, dataset and run name the way Langfuse does.

Requests: `GET dataset-items`, `GET dataset-items/{id}`, `GET projects`, `GET score-configs`,
`GET v2/datasets/{name}`, `POST otel/v1/traces`, `POST scores`, all under `<base URL>/api/public/`,
with a 60 second timeout each.

Errors name the HTTP status and what to check, never the response body.
