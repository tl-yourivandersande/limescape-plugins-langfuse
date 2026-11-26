"""Fase A van de Juridische Adviseur: de dossieranalyse nakijken met de vaste controles.

Twee acties. 'Dossier controleren' zet de uitkomst van de Limescape-agent om naar het
uitvoercontract en draait er de controles op (a_...-scores). 'Run samenvatten' rekent de twee
totalen van een run uit en vergelijkt de run met de goedgekeurde stand (baseline).

De controles zelf staan in _evaluators.py en _contract.py. Dat zijn gegenereerde kopieën uit de
evals-repository (scripts/sync_fase_a_checks.py); deze node voegt alleen de aansluiting op een
flow toe en rekent niets anders uit dan de bron.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from limescape_plugin_sdk import NodeContext, NodeInput, NodeInputError

from . import _evaluators
from ._contract import SLEUTEL, naar_contract
from ._evaluation import Evaluation
from ._version import EVALUATOR_VERSION, SOURCE_COMMIT

OPERATIONS = ("checks", "run_summary")
ANALYSIS_KEY = "analyse_afronden" + SLEUTEL
FORMATS = ("agent", "contract")


def _structured(inp: NodeInput, name: str) -> Any:
    value = inp.param(name)
    if isinstance(value, str):
        if not value.strip():
            return None
        try:
            return json.loads(value)
        except ValueError:
            raise NodeInputError(f"{name} is geen geldige JSON") from None
    return value


def _flag(value: Any) -> bool:
    return value is True or str(value or "").strip().lower() in {"1", "true", "yes", "ja", "on"}


def _version() -> dict[str, str]:
    return {"evaluator_versie": EVALUATOR_VERSION, "controles_commit": SOURCE_COMMIT}


# --- dossier controleren ------------------------------------------------------------


def _analysis(raw: Any) -> tuple[dict | None, str]:
    """De uitkomst van de agent, of waarom die er niet is.

    Accepteert de uitvoer van de agent-aanroep (met de sleutel van 'Analyse afronden eval' erin)
    en de analyse zelf. Een mislukte of lege aanroep geeft een fouttekst in plaats van een analyse.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None, "de agent gaf geen JSON terug"
    if not isinstance(raw, dict) or not raw:
        return None, "de agent gaf geen analyse terug"
    if ANALYSIS_KEY in raw:
        inner = raw[ANALYSIS_KEY]
        if isinstance(inner, str):
            try:
                inner = json.loads(inner)
            except ValueError:
                return None, f"{ANALYSIS_KEY} is geen JSON"
        raw = inner
    if isinstance(raw, dict) and any(key.endswith(SLEUTEL) for key in raw):
        return raw, ""
    error = raw.get("error") if isinstance(raw, dict) else None
    return None, f"de agent gaf een fout: {str(error)[:300]}" if error else f"de agent gaf geen {ANALYSIS_KEY} terug"


def _score(evaluation: Evaluation) -> dict[str, Any]:
    score = {"name": evaluation.name, "value": evaluation.value, "comment": evaluation.comment}
    if evaluation.data_type:
        score["data_type"] = evaluation.data_type
    if evaluation.metadata:
        score["metadata"] = evaluation.metadata
    return score


def _checks(ctx: NodeContext, inp: NodeInput) -> dict[str, Any]:
    item = _structured(inp, "item")
    if not isinstance(item, dict) or not isinstance(item.get("input"), dict):
        raise NodeInputError("item moet een dataset-item zijn met input, expected_output en metadata")
    item_input, expected = item["input"], item.get("expected_output")
    if not isinstance(item_input.get("documenten"), list) or not isinstance(expected, dict):
        raise NodeInputError("het dataset-item mist input.documenten of expected_output")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    dossier = metadata.get("dossier") or item.get("id")
    base = {"dossier": dossier, "dataset_item_id": item.get("id"), **_version()}

    analysis_format = str(inp.param("analysis_format") or "agent")
    if analysis_format not in FORMATS:
        raise NodeInputError(f"analysis_format moet een van {', '.join(FORMATS)} zijn")
    raw = _structured(inp, "analysis")
    if analysis_format == "contract":
        output, task_error = (raw, "") if isinstance(raw, dict) and raw else (None, "geen uitvoer ontvangen")
    else:
        analysis, task_error = _analysis(raw)
        try:
            output = naar_contract(analysis, SimpleNamespace(input=item_input)) if analysis is not None else None
        except (KeyError, TypeError, AttributeError) as exc:
            # De analyse heeft niet de vorm die de agent hoort te geven; dat is een mislukte taak.
            reason = f"de analyse is niet om te zetten naar het uitvoercontract ({type(exc).__name__})"
            output, task_error = None, reason
    if output is None:
        ctx.trace.event("juridische_adviseur_fase_a", {"operation": "checks", "dossier": dossier, "task_error": True})
        return {**base, "task_error": task_error, "output": None, "scores": [], "failed": [],
                "evaluator_errors": [], "non_negotiables_passed": False}

    scores: list[dict[str, Any]] = []
    evaluator_errors: list[str] = []
    for evaluator in _evaluators.ITEM_EVALUATORS:
        try:
            result = evaluator(input=item_input, output=output, expected_output=expected, metadata=metadata)
        except Exception as exc:  # noqa: BLE001 - één kapotte controle mag de andere niet tegenhouden
            evaluator_errors.append(f"{evaluator.__name__}: {type(exc).__name__}")
            continue
        scores.extend(_score(evaluation) for evaluation in (result if isinstance(result, list) else [result]))
    values = {score["name"]: score["value"] for score in scores}
    failed = sorted(name for name, value in values.items() if value is False)
    passed = all(values.get(name) is True for name in _evaluators.NON_NEGOTIABLES)
    ctx.trace.event(
        "juridische_adviseur_fase_a",
        {"operation": "checks", "dossier": dossier, "scores": len(scores), "non_negotiables_passed": passed},
    )
    return {**base, "task_error": "", "output": output, "scores": scores, "failed": failed,
            "evaluator_errors": evaluator_errors, "non_negotiables_passed": passed}


# --- run samenvatten ----------------------------------------------------------------


def _result(entry: Any) -> dict | None:
    """Het resultaat van 'Dossier controleren' in een element van de lus-uitvoer."""
    if isinstance(entry, dict) and "scores" in entry and "non_negotiables_passed" in entry:
        return entry
    if isinstance(entry, dict):
        for value in entry.values():
            found = _result(value)
            if found is not None:
                return found
    return None


def _run_summary(ctx: NodeContext, inp: NodeInput) -> dict[str, Any]:
    raw = _structured(inp, "results")
    entries = raw if isinstance(raw, list) else [raw] if raw else []
    results = [result for result in map(_result, entries) if result is not None]
    if not results:
        raise NodeInputError("results bevat geen enkel resultaat van 'Dossier controleren'")

    # Dezelfde twee totalen als de runner van de evals-repository, met dezelfde functies. Een dossier
    # waarvan de analyse mislukte telt daarin niet mee, net als bij de runner (die slaat een mislukte
    # taak over); het staat wel in failed_tasks en laat de gate falen.
    item_results = [
        SimpleNamespace(evaluations=[
            Evaluation(name=score["name"], value=score["value"], metadata=score.get("metadata"))
            for score in result["scores"]
        ])
        for result in results
        if not result.get("task_error")
    ]
    scores = [_score(evaluator(item_results=item_results)) for evaluator in _evaluators.RUN_EVALUATORS]
    per_dossier = {str(result["dossier"]): bool(result["non_negotiables_passed"]) for result in results}
    failed_tasks = sorted(str(result["dossier"]) for result in results if result.get("task_error"))

    expected = _structured(inp, "expected_items")
    if isinstance(expected, dict):
        expected = expected.get("items")
    wanted = [
        str((item.get("metadata") or {}).get("dossier") or item.get("id"))
        for item in expected or []
        if isinstance(item, dict)
    ]
    not_run = sorted(dossier for dossier in wanted if dossier not in per_dossier)

    baseline = _structured(inp, "baseline") or {}
    if not isinstance(baseline, dict):
        raise NodeInputError("baseline moet een JSON-object zijn, zoals fase-a/approved-baseline.json")
    approved = baseline.get("dossiers") if isinstance(baseline.get("dossiers"), dict) else {}
    # Terugval: een dossier dat in de goedgekeurde stand alle harde eisen haalde, haalt ze nu niet
    # of is niet gedraaid. Alleen dossiers die bij deze run horen tellen mee.
    in_scope = set(wanted) if wanted else set(per_dossier)
    regressions = sorted(
        f"{dossier}: niet gedraaid" if dossier not in per_dossier else f"{dossier}: haalde de non-negotiables, nu niet"
        for dossier, was_ok in approved.items()
        if dossier in in_scope and was_ok and per_dossier.get(dossier) is not True
    )
    ok = sum(per_dossier.values())
    summary = f"{ok}/{len(per_dossier)} dossiers halen alle harde eisen"
    if failed_tasks:
        summary += f"; analyse mislukt: {', '.join(failed_tasks)}"
    if not_run:
        summary += f"; niet gedraaid: {', '.join(not_run)}"
    if approved:
        summary += "; " + (
            "terugval t.o.v. baseline " + str(baseline.get("run") or "") + ": " + "; ".join(regressions)
            if regressions
            else "geen terugval t.o.v. baseline " + str(baseline.get("run") or "")
        )
    ctx.trace.event(
        "juridische_adviseur_fase_a",
        {"operation": "run_summary", "dossiers": len(per_dossier), "regressions": len(regressions)},
    )
    result = {
        **_version(),
        "scores": scores,
        "per_dossier": per_dossier,
        "failed_tasks": failed_tasks,
        "not_run": not_run,
        "baseline_run": baseline.get("run"),
        "regressions": regressions,
        "gate_passed": not regressions and not not_run and not failed_tasks,
        "summary": summary,
    }
    if _flag(inp.param("fail_on_regression")) and not result["gate_passed"]:
        raise NodeInputError(summary)
    return result


async def execute(ctx: NodeContext, inp: NodeInput):
    operation = str(inp.param("operation") or "checks")
    if operation not in OPERATIONS:
        raise NodeInputError(f"Onbekende actie '{operation}'")
    yield {inp.output_key: _checks(ctx, inp) if operation == "checks" else _run_summary(ctx, inp)}
