import base64
import hashlib
import json
import re
from pathlib import Path

from limescape_plugin_sdk import CapabilityError, NodeInput
from limescape_plugin_sdk.loader import load_execute
from limescape_plugin_sdk.testing import FakeContext, run_node
from limescape_plugin_sdk.testing.runner import run_execute

NODE_DIR = Path(__file__).resolve().parents[1]
BASE = "https://langfuse.example.test"
PUBLIC, SECRET = "pk-lf-synthetic", "sk-lf-synthetic-secret"
PROJECT, DATASET_ID = "project-1", "dataset-1"

# What the Langfuse Python SDK 5.2.0 puts on the two spans of an experiment item
# (captured from run_experiment against a recording server). The node must send at least these.
SDK_TASK_SPAN_KEYS = {
    "langfuse.observation.input",
    "langfuse.observation.output",
    "langfuse.observation.type",
    "langfuse.environment",
    "langfuse.experiment.description",
    "langfuse.experiment.item.expected_output",
    "langfuse.experiment.id",
    "langfuse.experiment.name",
    "langfuse.experiment.metadata",
    "langfuse.experiment.dataset.id",
    "langfuse.experiment.item.id",
    "langfuse.experiment.item.metadata",
    "langfuse.experiment.item.root_observation_id",
    "langfuse.observation.metadata.experiment_run_name",
    "langfuse.observation.metadata.dataset_id",
    "langfuse.observation.metadata.dataset_item_id",
}


def row(number: int, *, split: str = "ontwikkel", status: str = "ACTIVE") -> dict:
    return {
        "id": f"item-{number}",
        "status": status,
        "datasetId": DATASET_ID,
        "datasetName": "voorbeeld-set",
        "input": {"zaak": {"zaaknummer": f"9000{number}"}, "documenten": [{"id": f"D0{number}-01", "tekst": "t"}]},
        "expectedOutput": {"antwoord": number},
        "metadata": {"dossier": f"D0{number}", "split": split},
    }


ITEM = {
    "id": "item-1",
    "dataset_id": DATASET_ID,
    "dataset_name": "voorbeeld-set",
    "input": {"vraag": "a"},
    "expected_output": {"antwoord": "b"},
    "metadata": {"dossier": "D01", "split": "ontwikkel"},
}


class Server:
    """A recording stand-in for the Langfuse public API."""

    def __init__(self, routes: dict | None = None):
        self.requests: list[dict] = []
        self.routes = routes or {}

    def __call__(self, request: dict) -> dict:
        self.requests.append(request)
        path = request["url"].removeprefix(f"{BASE}/api/public/")
        handler = self.routes.get((request["method"], path))
        if handler is None:
            return {"status": 404, "json": {"message": "not found"}}
        return handler(request) if callable(handler) else handler

    def sent(self, method: str, path: str) -> list[dict]:
        return [r for r in self.requests if r["method"] == method and r["url"] == f"{BASE}/api/public/{path}"]


def context(server: Server, **credential_values) -> FakeContext:
    values = {"baseUrl": BASE + "/", "publicKey": PUBLIC, "secretKey": SECRET, **credential_values}
    return FakeContext(
        granted=["credentials", "http"],
        credential_resolver=lambda reference, **kwargs: {"reference": reference, "type_name": "langfuseApi", "values": values},
        http_handler=server,
    )


def experiment_server(extra: dict | None = None) -> Server:
    return Server(
        {
            ("GET", "projects"): {"status": 200, "json": {"data": [{"id": PROJECT, "name": "evals"}]}},
            ("GET", "score-configs"): {
                "status": 200,
                "json": {
                    "data": [
                        {"id": "cfg-goed", "name": "a_goed", "dataType": "BOOLEAN", "isArchived": False},
                        {"id": "cfg-oud", "name": "a_recall", "dataType": "NUMERIC", "isArchived": True},
                    ],
                    "meta": {"page": 1, "limit": 50, "totalItems": 2, "totalPages": 1},
                },
            },
            ("POST", "otel/v1/traces"): {"status": 200, "json": {}},
            ("POST", "scores"): {"status": 202, "json": {}},
            **(extra or {}),
        }
    )


def attributes(span: dict) -> dict:
    return {a["key"]: next(iter(a["value"].values())) for a in span["attributes"]}


def expected_experiment_id(run_name: str) -> str:
    payload = json.dumps(["langfuse-experiment-v1", PROJECT, DATASET_ID, run_name], separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


async def test_dataset_items_follows_pages_filters_and_uses_the_team_credential():
    """All pages are read; archived items and items outside the metadata filter are left out."""
    pages = {
        1: {"data": [row(2), row(1, split="test")], "meta": {"page": 1, "limit": 50, "totalItems": 4, "totalPages": 2}},
        2: {"data": [row(3), row(4, status="ARCHIVED")], "meta": {"page": 2, "limit": 50, "totalItems": 4, "totalPages": 2}},
    }
    server = Server({("GET", "dataset-items"): lambda request: {"status": 200, "json": pages[request["params"]["page"]]}})
    ctx = context(server)

    result = await run_node(
        NODE_DIR, ctx,
        params={"operation": "dataset_items", "dataset_name": "voorbeeld-set",
                "metadata_filter": '{"split": "ontwikkel"}', "dataset_version": "2026-10-27T07:52:00Z"},
        output_key="dataset",
    )

    assert ctx.calls[0].request == {"reference": "langfuse_api", "mode": "team", "expected_type": "langfuseApi", "oauth2": False}
    first = server.requests[0]
    assert first["params"] == {"datasetName": "voorbeeld-set", "version": "2026-10-27T07:52:00.000000Z", "page": 1, "limit": 50}
    assert first["headers"]["Authorization"] == "Basic " + base64.b64encode(f"{PUBLIC}:{SECRET}".encode()).decode()
    dataset = result[0]["dataset"]
    assert [item["id"] for item in dataset["items"]] == ["item-2", "item-3"]
    assert dataset["items"][0]["input"]["zaak"] == {"zaaknummer": "90002"}
    assert dataset["items"][0]["expected_output"] == {"antwoord": 2}
    assert (dataset["count"], dataset["dataset_id"], dataset["dataset_version"]) == (2, DATASET_ID, "2026-10-27T07:52:00.000000Z")
    assert re.fullmatch(r"voorbeeld-set-20\d{6}-\d{6}", dataset["run_name"])
    assert SECRET not in json.dumps(result)


async def test_dataset_items_takes_the_selection_from_the_flow_input_and_can_leave_out_content():
    """A run chooses its dossiers through the flow input; without content only id and metadata come back."""
    server = Server({("GET", "dataset-items"): {"status": 200, "json": {
        "data": [row(1), row(2), row(3)], "meta": {"page": 1, "limit": 50, "totalItems": 3, "totalPages": 1}}}})

    result = await run_node(
        NODE_DIR, context(server),
        params={"operation": "dataset_items", "dataset_name": "voorbeeld-set", "metadata_filter": "{}",
                "include_content": "0", "run_name": "vaste-naam"},
        input_data={"metadata_filter": {"dossier": ["D01", "D03"], "split": ""}, "run_name": "dossier-analyse-v7-{timestamp}"},
        output_key="dataset",
    )

    dataset = result[0]["dataset"]
    assert dataset["items"] == [
        {"id": "item-1", "dataset_id": DATASET_ID, "dataset_name": "voorbeeld-set", "metadata": {"dossier": "D01", "split": "ontwikkel"}},
        {"id": "item-3", "dataset_id": DATASET_ID, "dataset_name": "voorbeeld-set", "metadata": {"dossier": "D03", "split": "ontwikkel"}},
    ]
    assert re.fullmatch(r"dossier-analyse-v7-20\d{6}-\d{6}", dataset["run_name"])


async def test_dataset_items_without_a_match_fails_instead_of_running_an_empty_experiment():
    server = Server({("GET", "dataset-items"): {"status": 200, "json": {
        "data": [row(1)], "meta": {"page": 1, "limit": 50, "totalItems": 1, "totalPages": 1}}}})

    result = await run_node(NODE_DIR, context(server), params={
        "operation": "dataset_items", "dataset_name": "voorbeeld-set", "metadata_filter": '{"split": "test"}'})

    assert result == [{"error": "Dataset 'voorbeeld-set' has no active items that match the selection"}]


async def test_dataset_item_also_puts_the_input_fields_in_the_flow_state():
    """With a prefix an agent call can take the item input from named flow-state keys."""
    server = Server({("GET", "dataset-items/item%2F1"): {"status": 200, "json": {**row(1), "id": "item/1"}}})

    result = await run_node(
        NODE_DIR, context(server),
        params={"operation": "dataset_item", "item_id": "item/1", "state_key_prefix": "eval_"},
        output_key="dataset_item_ophalen",
    )

    state = result[0]
    assert set(state) == {"dataset_item_ophalen", "eval_zaak", "eval_documenten"}
    assert state["eval_zaak"] == {"zaaknummer": "90001"}
    assert state["dataset_item_ophalen"]["dataset_id"] == DATASET_ID
    assert state["dataset_item_ophalen"]["fetched_at"].endswith("Z")


async def test_next_item_steps_through_the_list_by_iteration_without_calling_langfuse():
    execute = load_execute(NODE_DIR, NODE_DIR.name)
    items = [{"id": "item-1", "metadata": {"dossier": "D01"}}, {"id": "item-2", "metadata": {"dossier": "D02"}}]
    ctx = FakeContext(granted=["credentials", "http"])

    async def step(iteration):
        node_input = NodeInput(code=NODE_DIR.name, params={"operation": "next_item", "items": items},
                               output_key="volgend", iteration=iteration)
        return (await run_execute(execute, ctx, node_input))[0]["volgend"]

    assert await step(1) == {"done": False, "index": 1, "total": 2, "item": items[0], "item_id": "item-1"}
    assert (await step(2))["item_id"] == "item-2"
    assert await step(3) == {"done": True, "index": 3, "total": 2, "item": None, "item_id": None}
    assert ctx.calls == []


async def test_experiment_item_sends_the_trace_the_sdk_sends_and_scores_on_the_task_span():
    """One trace per item, linked to the experiment through span attributes, with its scores."""
    server = experiment_server()

    result = await run_node(
        NODE_DIR, context(server),
        params={
            "operation": "experiment_item", "run_name": "dossier-analyse-v7", "run_description": "Fase A",
            "run_metadata": {"fase": "A", "evaluator_versie": "abc123"}, "trace_name": "dossier-analyse D01",
            "item": ITEM, "output": {"antwoord": "b"}, "dataset_version": "2026-10-27T07:52:00Z",
            "started_at": "2026-10-27T08:00:00Z",
            "scores": [
                {"name": "a_goed", "value": True, "comment": "klopt"},
                {"name": "a_recall", "value": 0.5, "comment": "1/2", "metadata": {"gevonden": 1}},
            ],
        },
        output_key="vastgelegd",
    )

    out = result[0]["vastgelegd"]
    experiment = expected_experiment_id("dossier-analyse-v7")
    trace_request = server.sent("POST", "otel/v1/traces")[0]
    assert trace_request["headers"]["x-langfuse-ingestion-version"] == "4"
    assert trace_request["headers"]["Content-Type"] == "application/json"
    scope_spans = trace_request["json"]["resourceSpans"][0]["scopeSpans"][0]
    assert scope_spans["scope"]["name"] == "langfuse-sdk"
    assert attributes(scope_spans["scope"]) == {"public_key": PUBLIC}
    spans = {span["name"]: span for span in scope_spans["spans"]}
    root, task = spans["experiment-item-run"], spans["experiment-item-task"]
    assert "parentSpanId" not in root and task["parentSpanId"] == root["spanId"]
    assert root["traceId"] == task["traceId"] == out["trace_id"] and len(out["trace_id"]) == 32
    assert int(root["startTimeUnixNano"]) == 1793088000 * 10**9 <= int(root["endTimeUnixNano"])

    for span in (root, task):
        got = attributes(span)
        assert SDK_TASK_SPAN_KEYS <= set(got)
        assert got["langfuse.experiment.id"] == experiment == out["experiment_id"]
        assert got["langfuse.experiment.name"] == "dossier-analyse-v7"
        assert got["langfuse.experiment.dataset.id"] == DATASET_ID
        assert got["langfuse.experiment.item.id"] == "item-1"
        assert got["langfuse.experiment.item.root_observation_id"] == task["spanId"] == out["observation_id"]
        assert got["langfuse.experiment.item.version"] == "2026-10-27T07:52:00.000000Z"
        assert json.loads(got["langfuse.experiment.metadata"]) == {"fase": "A", "evaluator_versie": "abc123"}
        assert json.loads(got["langfuse.experiment.item.metadata"]) == {"dossier": "D01", "split": "ontwikkel"}
        assert json.loads(got["langfuse.observation.input"]) == {"vraag": "a"}
        assert json.loads(got["langfuse.observation.output"]) == {"antwoord": "b"}
        assert json.loads(got["langfuse.experiment.item.expected_output"]) == {"antwoord": "b"}
        assert got["langfuse.observation.metadata.dossier"] == '"D01"'
        assert got["langfuse.trace.name"] == "dossier-analyse D01"
        assert got["langfuse.environment"] == "sdk-experiment"
    assert attributes(root)["langfuse.internal.is_app_root"] is True

    scores = server.sent("POST", "scores")[0]["json"]
    assert [(s["name"], s["value"], s.get("dataType"), s.get("configId")) for s in scores] == [
        ("a_goed", 1.0, "BOOLEAN", "cfg-goed"),
        ("a_recall", 0.5, None, None),  # its config is archived, so it is not linked
    ]
    assert all(s["traceId"] == out["trace_id"] and s["observationId"] == task["spanId"] for s in scores)
    assert scores[1]["metadata"] == {"gevonden": 1} and scores[0]["comment"] == "klopt"
    assert len({s["id"] for s in scores}) == 2
    assert out["scores_written"] == 2
    assert out["trace_url"] == f"{BASE}/project/{PROJECT}/traces/{out['trace_id']}"
    assert SECRET not in json.dumps(result)


async def test_experiment_item_marks_a_failed_task_as_error():
    server = experiment_server()

    await run_node(NODE_DIR, context(server), params={
        "operation": "experiment_item", "run_name": "run", "item": ITEM, "output": "",
        "error_message": "agent status failed bij node Tijdlijn", "score_configs": "ignore"})

    task = attributes(server.sent("POST", "otel/v1/traces")[0]["json"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0])
    assert task["langfuse.observation.level"] == "ERROR"
    assert task["langfuse.observation.output"] == "Error: agent status failed bij node Tijdlijn"
    assert server.sent("GET", "score-configs") == [] and server.sent("POST", "scores") == []


async def test_experiment_item_can_require_a_score_config_and_then_writes_nothing():
    server = experiment_server()

    result = await run_node(NODE_DIR, context(server), params={
        "operation": "experiment_item", "run_name": "run", "item": ITEM, "output": {"a": 1},
        "scores": [{"name": "a_onbekend", "value": True}], "score_configs": "require"})

    assert result == [{"error": "Langfuse has no score config for: a_onbekend"}]
    assert server.sent("POST", "otel/v1/traces") == []


async def test_experiment_scores_attach_to_the_experiment_of_dataset_and_run_name():
    server = experiment_server({("GET", "v2/datasets/voorbeeld-set"): {"status": 200, "json": {"id": DATASET_ID, "name": "voorbeeld-set"}}})

    result = await run_node(
        NODE_DIR, context(server),
        params={"operation": "experiment_scores", "dataset_name": "voorbeeld-set", "run_name": "dossier-analyse-v7",
                "scores": '[{"name": "a_totaal", "value": 0.8, "comment": "4/5"}]', "score_configs": "ignore"},
        output_key="run_scores",
    )

    experiment = expected_experiment_id("dossier-analyse-v7")
    score = server.sent("POST", "scores")[0]["json"][0]
    assert (score["datasetRunId"], score["name"], score["value"], score["comment"]) == (experiment, "a_totaal", 0.8, "4/5")
    assert "traceId" not in score
    assert result[0]["run_scores"]["experiment_id"] == experiment


async def test_experiment_scores_use_a_given_experiment_id_without_looking_up_the_dataset():
    server = experiment_server()

    await run_node(NODE_DIR, context(server), params={
        "operation": "experiment_scores", "experiment_id": "abcdef0123456789",
        "scores": [{"name": "a_totaal", "value": 1}], "score_configs": "ignore"})

    assert server.sent("POST", "scores")[0]["json"][0]["datasetRunId"] == "abcdef0123456789"
    assert all("datasets" not in request["url"] for request in server.requests)


async def test_rejected_scores_fail_the_node():
    server = experiment_server({("POST", "scores"): {"status": 207, "json": {"accepted": 0, "rejected": 1, "errors": []}}})

    result = await run_node(NODE_DIR, context(server), params={
        "operation": "experiment_scores", "experiment_id": "abcdef0123456789",
        "scores": [{"name": "a_totaal", "value": 1}], "score_configs": "ignore"})

    assert result == [{"error": "Langfuse rejected 1 of 1 scores; check names, values and configs"}]


async def test_missing_team_credential_gives_an_actionable_message():
    def refuse(reference, **kwargs):
        raise CapabilityError("credentials", "No active credential matches", code="not_found")

    ctx = FakeContext(granted=["credentials", "http"], credential_resolver=refuse)

    result = await run_node(NODE_DIR, ctx, params={"operation": "dataset_items", "dataset_name": "x"})

    assert result == [{"error": "This team has no active Langfuse credential. Configure Langfuse under Team settings"}]
    assert not any(call.capability == "http" for call in ctx.calls)


async def test_http_errors_are_reported_without_the_response_body_or_the_keys():
    server = Server({("GET", "dataset-items"): {"status": 401, "json": {"message": f"Invalid credentials {SECRET}"}}})

    result = await run_node(NODE_DIR, context(server), params={"operation": "dataset_items", "dataset_name": "x"})

    assert result == [{"error": "Langfuse refused the keys. Check the public and secret key under Team settings "
                                "(HTTP 401 on GET dataset-items)"}]


async def test_a_stopped_langfuse_that_answers_with_a_web_page_is_named_as_such():
    server = Server({("GET", "dataset-items"): {"status": 200, "text": "<html>Container App is stopped</html>"}})

    result = await run_node(NODE_DIR, context(server), params={"operation": "dataset_items", "dataset_name": "x"})

    assert result == [{"error": "Langfuse did not return JSON. Check the base URL and whether Langfuse is running"}]


async def test_a_credential_without_a_usable_url_is_refused_before_any_request():
    server = Server()

    result = await run_node(NODE_DIR, context(server, baseUrl="langfuse.example.test"),
                            params={"operation": "dataset_items", "dataset_name": "x"})

    assert result == [{"error": "The Langfuse credential has no valid base URL"}]
    assert server.requests == []
