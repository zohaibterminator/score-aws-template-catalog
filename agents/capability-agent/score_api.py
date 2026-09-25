"""Calls the score-api CGI endpoints (/cgi-bin/score, /cgi-bin/update-aws-creds, /cgi-bin/delete-all).

score-api answers HTTP 200 with {"status": "ok" | "partial" | "dry_run" | "error", ...} in the body, so the
status field, not the HTTP code, says whether the operation worked.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

# The same rules score-api applies, checked here so a bad request fails before it reaches the cluster.
WORKLOAD = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")
IMAGE = re.compile(r"^[A-Za-z0-9._/:@-]+$")
ACCESS_KEY_ID = re.compile(r"^[A-Z0-9]{16,32}$")
SECRET_ACCESS_KEY = re.compile(r"^[A-Za-z0-9/+=]{30,60}$")
DELETE_CONFIRMATION = "DELETE-ALL"
# score-api reads these params at the top level of the request body; the rest go under "db".
TOP_LEVEL_PARAMS = {"plane"}


class ScoreApiError(Exception):
    """score-api could not be reached, or did not answer with JSON."""


class ScoreApi:
    def __init__(self, base_url: str, app_secret: str, timeout: float = 1500, verify: bool = True,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base_url, self.app_secret = base_url.rstrip("/"), app_secret
        self.timeout, self.verify, self.transport = timeout, verify, transport

    async def _post(self, endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.base_url}/cgi-bin/{endpoint}"
        # delete-all holds the connection open until every RDS instance is destroyed (5-20 minutes).
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=10), verify=self.verify,
                                     transport=self.transport) as client:
            try:
                response = await client.post(url, json=body, headers={"X-App-Secret": self.app_secret})
            except httpx.HTTPError as exc:
                raise ScoreApiError(f"could not reach {url}: {type(exc).__name__}: {exc}") from exc
        try:
            result = response.json()
        except ValueError:
            raise ScoreApiError(f"{url} answered HTTP {response.status_code} without JSON: "
                                f"{response.text[:300]!r}") from None
        if not isinstance(result, dict) or "status" not in result:
            raise ScoreApiError(f"{url} answered HTTP {response.status_code} with unexpected JSON: {result!r}")
        return result

    async def score(self, workload: str, image: str, params: dict[str, Any]) -> dict[str, Any]:
        body: dict[str, Any] = {"workload": workload, "image": image, "db": {}}
        for name, value in params.items():
            (body if name in TOP_LEVEL_PARAMS else body["db"])[name] = value
        return await self._post("score", body)

    async def update_aws_creds(self, access_key_id: str, secret_access_key: str, region: str) -> dict[str, Any]:
        return await self._post("update-aws-creds", {"access_key_id": access_key_id,
                                                     "secret_access_key": secret_access_key, "region": region})

    async def delete_all(self, confirm: str | None) -> dict[str, Any]:
        return await self._post("delete-all", {"confirm": confirm or ""})
