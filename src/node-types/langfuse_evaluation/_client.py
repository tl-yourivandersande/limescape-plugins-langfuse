"""Bounded calls to the Langfuse public API through ``ctx.http``, with safe errors.

The keys come from the team's Langfuse credential (type ``langfuseApi``), the one Studio
stores under Team settings > Langfuse. They are used for the Basic auth header only and
never appear in output, logs or error messages.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote, urlsplit

from limescape_plugin_sdk import CapabilityError, NodeContext, NodeInputError

CREDENTIAL_TYPE = "langfuseApi"
# Code of the credential that Team settings > Langfuse creates.
TEAM_SETTINGS_CODE = "langfuse_api"
TIMEOUT_SECONDS = 60
PAGE_SIZE = 50
MAX_PAGES = 40  # 2000 items or score configs; more is refused instead of silently cut off

# Platform refusals (CapabilityError.code) as a safe, actionable message.
REFUSALS = {
    "not_found": "This team has no active Langfuse credential. Configure Langfuse under Team settings",
    "wrong_type": "Select a Langfuse credential",
    "ambiguous": "More than one Langfuse credential matches the selection",
    "no_scope": "No team is known for this run",
}
HTTP_REASONS = {
    400: "Langfuse refused the request as invalid",
    401: "Langfuse refused the keys. Check the public and secret key under Team settings",
    403: "The Langfuse keys have no access to this project or feature",
    404: "Langfuse did not find this. Check the name or id, and whether Langfuse is running at the configured URL",
    429: "Langfuse is rate limiting. Try again later",
}


def segment(value: Any, label: str) -> str:
    """A value that is safe as one URL path segment."""
    if not isinstance(value, str) or not value.strip() or value.strip() in {".", ".."}:
        raise NodeInputError(f"{label} is required")
    return quote(value.strip(), safe="")


class Langfuse:
    """One Langfuse project, reached with the keys of a single credential."""

    def __init__(self, ctx: NodeContext, base_url: str, public_key: str, secret_key: str) -> None:
        self._http = ctx.http
        self.base_url = base_url
        self.public_key = public_key
        token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode("ascii")
        self._authorization = f"Basic {token}"

    @classmethod
    async def connect(cls, ctx: NodeContext, reference: Any) -> Langfuse:
        """Resolve the team's Langfuse credential; an empty selection means the team settings."""
        code = reference.strip() if isinstance(reference, str) and reference.strip() else TEAM_SETTINGS_CODE
        try:
            credential = await ctx.credentials.resolve(code, mode="team", expected_type=CREDENTIAL_TYPE)
        except CapabilityError as exc:
            if exc.code in REFUSALS:
                raise NodeInputError(REFUSALS[exc.code]) from None
            raise NodeInputError("The Langfuse credential could not be read") from None
        values = credential.values
        base_url, public_key, secret_key = (
            str(values.get(key) or "").strip() for key in ("baseUrl", "publicKey", "secretKey")
        )
        parts = urlsplit(base_url)
        if parts.scheme not in {"http", "https"} or not parts.netloc or parts.query or parts.fragment:
            raise NodeInputError("The Langfuse credential has no valid base URL")
        if not public_key or not secret_key:
            raise NodeInputError("The Langfuse credential has no public and secret key")
        return cls(ctx, base_url.rstrip("/"), public_key, secret_key)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
        accept: tuple[int, ...] = (),
    ) -> Any:
        """Call ``/api/public/<path>`` and return the decoded JSON body.

        A status in ``accept`` is returned as ``None`` instead of raised, for calls where
        "not there" is an answer.
        """
        url = f"{self.base_url}/api/public/{path}"
        query = {key: value for key, value in (params or {}).items() if value not in (None, "")}
        try:
            response = await self._http.request(
                method,
                url,
                headers={"Authorization": self._authorization, "Accept": "application/json", **(headers or {})},
                params=query or None,
                json=body,
                timeout=TIMEOUT_SECONDS,
            )
        except CapabilityError as exc:
            if exc.code == "timeout":
                raise NodeInputError(f"Langfuse did not answer within {TIMEOUT_SECONDS} seconds") from None
            raise NodeInputError("Langfuse could not be reached at the configured URL") from None
        if response.status in accept:
            return None
        if not 200 <= response.status < 300:
            reason = HTTP_REASONS.get(response.status, "Langfuse returned an error")
            raise NodeInputError(f"{reason} (HTTP {response.status} on {method} {path.split('?')[0]})")
        if not response.body:
            return None
        try:
            return response.json()
        except ValueError:
            raise NodeInputError(
                "Langfuse did not return JSON. Check the base URL and whether Langfuse is running"
            ) from None

    async def pages(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """All rows of a paginated list (``data`` + ``meta.totalPages``), bounded by MAX_PAGES."""
        rows: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            result = await self.request("GET", path, params={**(params or {}), "page": page, "limit": PAGE_SIZE})
            data = result.get("data") if isinstance(result, dict) else None
            if not isinstance(data, list):
                raise NodeInputError(f"Langfuse returned an unexpected answer for {path}")
            rows.extend(row for row in data if isinstance(row, dict))
            meta = result.get("meta") if isinstance(result.get("meta"), dict) else {}
            total_pages = meta.get("totalPages")
            if not data or not isinstance(total_pages, int) or page >= total_pages:
                return rows
        raise NodeInputError(f"More than {MAX_PAGES * PAGE_SIZE} rows for {path}; narrow the selection")

    async def project_id(self) -> str:
        """The project these keys belong to; a key pair is always scoped to one project."""
        result = await self.request("GET", "projects")
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list) or not data or not isinstance(data[0], dict) or not data[0].get("id"):
            raise NodeInputError("Langfuse did not return the project of these keys")
        return str(data[0]["id"])
