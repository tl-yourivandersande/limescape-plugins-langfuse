# Juridische Adviseur: fase A checks

Checks a fase A dossier analysis (an overview of a dossier without a legal verdict) against the fixed
rules of the evaluation set, and summarises a run. The node makes no external calls and uses no model.

The checks themselves are maintained in the evals repository. `_evaluators.py`, `_contract.py`,
`_version.py` and `uitvoercontract.json` in this folder are a generated copy
(`scripts/sync_fase_a_checks.py`); the node only connects them to a flow.

## Check one dossier

Input: the dataset item (`input.zaak`, `input.documenten`, `expected_output`, `metadata`) and the dossier
analysis. With format *Output of the Limescape agent* the node takes the output of the agent call, finds
the result of `Analyse afronden eval` in it and converts it to the output contract first. With *Already in
the output contract* the analysis is checked as it is.

Result:

```
{
  "dossier": "D01", "dataset_item_id": "...",
  "evaluator_versie": "...", "controles_commit": "...",
  "task_error": "",
  "output": { ...the analysis in the output contract... },
  "scores": [{"name": "a_geen_kernoordeel", "value": true, "data_type": "BOOLEAN", "comment": "..."}, ...],
  "failed": ["a_..."],
  "evaluator_errors": [],
  "non_negotiables_passed": true
}
```

There are seventeen checks and eighteen scores (`a_grondslagen_recall` is a number). Eight checks are the
hard requirements; `non_negotiables_passed` is true only when all eight are true. `output` and `scores`
are what **Record experiment result** of the Langfuse node takes.

When the agent call failed or returned no analysis, `task_error` says why, there are no scores and
`non_negotiables_passed` is false. Pass `task_error` to the Langfuse node as the error message.

## Summarise the run

Input: the results of **Check one dossier** for all dossiers of the run (the output of the agent call in
the loop works as it is), the dataset items of the run and, optionally, the approved baseline (the content
of `fase-a/approved-baseline.json`).

Result: the two run scores (`a_non_negotiables_geslaagd`, `a_grondslagen_recall_totaal`) for
**Write scores on the experiment**, `per_dossier`, `failed_tasks`, `not_run`, `regressions`,
`gate_passed` and a one-line `summary`.

A regression is a dossier that met all hard requirements in the baseline and does not now, or did not
run. Only dossiers that belong to this run count, so a run on a few dossiers is compared on those few.
Like the runner in the evals repository, a dossier whose analysis failed is left out of the two run
scores; it is listed in `failed_tasks` and makes `gate_passed` false. With **Fail on regression** the
node fails instead, which stops the flow.

## Not in this node

The signal `a_signaal_bevinding_in_bron` (a decision model that checks every finding against its source)
and the scores a jurist gives (`m_...`) are not computed here.
