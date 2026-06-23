import asyncio
import logging
import os
import time
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://login.salesforce.com/services/oauth2/token"
_CLIENT_ID = "3MVG9nSH73I5aFNi1._.oYzqFFlQX7QCSbG7NKSXbytZQQ3gE9A.XzpOme5Luew3GXmNc9fbhZdVLGq_JyN7g"
_REFRESH_BUFFER_SECONDS = 300  # refresh 5 min before expiry


class TokenManager:
    def __init__(self) -> None:
        self._client_secret: str = os.environ["SALESFORCE_CLIENT_SECRET"]
        self._refresh_token: str = os.environ["SALESFORCE_REFRESH_TOKEN"]
        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()
        self._http: Optional[httpx.AsyncClient] = None
        self.instance_url: str = ""
        self.token_scope: str = ""

    async def initialize(self) -> None:
        self._http = httpx.AsyncClient(timeout=30.0)
        await self._do_refresh()
        logger.info("Salesforce token initialized successfully")

    async def close(self) -> None:
        if self._http:
            await self._http.aclose()

    async def get_access_token(self) -> str:
        if time.time() >= self._expires_at - _REFRESH_BUFFER_SECONDS:
            async with self._lock:
                # Double-check inside lock to avoid thundering herd
                if time.time() >= self._expires_at - _REFRESH_BUFFER_SECONDS:
                    await self._do_refresh()
        assert self._access_token is not None
        return self._access_token

    async def _do_refresh(self) -> None:
        logger.info("Refreshing Salesforce access token")
        try:
            response = await self._http.post(
                _TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": _CLIENT_ID,
                    "client_secret": self._client_secret,
                    "refresh_token": self._refresh_token,
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(
                "Token refresh failed: %s %s", exc.response.status_code, exc.response.text
            )
            raise

        data = response.json()
        self._access_token = data["access_token"]
        self.instance_url = data.get("instance_url", "")
        self.token_scope = data.get("scope", "")
        # Salesforce tokens default to 2 hours; some orgs omit expires_in
        expires_in: int = data.get("expires_in", 7200)
        self._expires_at = time.time() + expires_in
        logger.info("Access token refreshed; expires in %ds, scope=%s", expires_in, self.token_scope)
