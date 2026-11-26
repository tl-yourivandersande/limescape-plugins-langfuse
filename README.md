# limescape-plugins-langfuse

Limescape plugin node types for running evaluations with [Langfuse](https://langfuse.com) from a flow:
read a dataset, run a solution on every item, score the result and record it as an experiment.

- Bundle key: `langfuse` (confirm with a Limescape administrator before registration).
- Node types live under `src/node-types/<code>/`.

| Node type | What it does |
|---|---|
| [`langfuse_evaluation`](src/node-types/langfuse_evaluation/README.md) | Dataset items, experiment results and scores in Langfuse. Uses the Langfuse connection from the team settings. |
| [`juridische_adviseur_fase_a`](src/node-types/juridische_adviseur_fase_a/README.md) | The fixed checks for a fase A dossier analysis of the Juridische Adviseur, and the run summary with the regression check. |

This repository is public. Never commit customer data, real identifiers, dossier text, credentials or
secrets. Tests use synthetic dossiers only.

## How the node types fit together

A run is two flows. The first reads the dataset and loops over the items; the second handles one item:

```
run flow                                     item flow (called once per item)
────────                                     ────────────────────────────────
Langfuse: get dataset items (ids only)       Langfuse: get one dataset item
  └─ loop: Langfuse: next item               agent call: the solution under test
       └─ agent call: item flow              fase A checks: check one dossier
fase A checks: summarise the run             Langfuse: record experiment result
Langfuse: write scores on the experiment
```

Keeping one item per flow run keeps the flow state small: the run flow only carries ids, metadata and
scores, never dossier text.

## Development

```
uv sync
uv run limescape-plugin validate
uv run pytest
uv run ruff check src scripts
```

The checks of `juridische_adviseur_fase_a` are a generated copy from the evals repository. After a change
there, regenerate and verify:

```
uv run python scripts/sync_fase_a_checks.py <path to juridische-adviseur-evals>
EVALS_REPO=<path to juridische-adviseur-evals> uv run pytest
```

## Release

A release is made by merging reviewed code to `main` and tagging `vX.Y.Z`. The release workflow builds
the bundle and puts the activation checksum in the release notes. Limescape administrators register the
bundle source and activate each release; contributors do not merge or activate releases.

The repository's licence must be decided before distributing a release.
