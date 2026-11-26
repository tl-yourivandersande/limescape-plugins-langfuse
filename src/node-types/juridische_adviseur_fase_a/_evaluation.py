"""The result of one check: the fields of the Langfuse SDK's ``Evaluation`` the checks use.

The checks in ``_evaluators.py`` are written against the Langfuse SDK. The node only needs
this record, so the bundle does not have to carry the SDK.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class Evaluation:
    name: str
    value: Any
    comment: str | None = None
    metadata: dict[str, Any] | None = None
    data_type: str | None = None
    config_id: str | None = None
