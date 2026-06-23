import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse

from token_manager import TokenManager

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

SALESFORCE_MCP_URL = os.getenv(
    "SALESFORCE_MCP_URL",
    "https://api.salesforce.com/platform/mcp/v1/platform/sobject-all",
)
PEGA_EXPECTED_TOKEN = os.getenv("PEGA_EXPECTED_TOKEN")

_HOP_BY_HOP = frozenset([
    "host", "content-length", "transfer-encoding", "connection",
    "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "upgrade",
])

_token_manager: TokenManager


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _token_manager
    _token_manager = TokenManager()
    await _token_manager.initialize()
    yield
    await _token_manager.close()


app = FastAPI(title="Salesforce MCP Proxy", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


def _upstream_headers(request: Request, sf_token: str) -> dict[str, str]:
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP and k.lower() not in ("authorization", "accept")
    }
    headers["Authorization"] = f"Bearer {sf_token}"
    headers["Accept"] = "application/json, text/event-stream"
    return headers


def _downstream_headers(sf_headers: httpx.Headers) -> dict[str, str]:
    return {
        k: v
        for k, v in sf_headers.items()
        if k.lower() not in _HOP_BY_HOP
    }


def _check_pega_auth(request: Request) -> bool:
    if not PEGA_EXPECTED_TOKEN:
        return True
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    return token == PEGA_EXPECTED_TOKEN


async def _proxy(request: Request) -> Response:
    if not _check_pega_auth(request):
        return Response(status_code=401, content=b"Unauthorized")

    sf_token = await _token_manager.get_access_token()
    req_headers = _upstream_headers(request, sf_token)
    body = await request.body()

    logger.info(
        "Proxying %s /mcp (session=%s)",
        request.method,
        request.headers.get("Mcp-Session-Id", "-"),
    )

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
        follow_redirects=True,
    )

    sf_req = client.build_request(
        method=request.method,
        url=SALESFORCE_MCP_URL,
        headers=req_headers,
        content=body,
    )

    try:
        sf_resp = await client.send(sf_req, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        logger.error("Upstream request failed: %s", exc)
        return Response(status_code=502, content=b"Bad Gateway: upstream request failed")

    logger.info(
        "Salesforce responded %d (content-type=%s)",
        sf_resp.status_code,
        sf_resp.headers.get("content-type", ""),
    )

    async def generate() -> AsyncIterator[bytes]:
        try:
            async for chunk in sf_resp.aiter_bytes():
                yield chunk
        finally:
            await sf_resp.aclose()
            await client.aclose()

    return StreamingResponse(
        generate(),
        status_code=sf_resp.status_code,
        headers=_downstream_headers(sf_resp.headers),
        media_type=sf_resp.headers.get("content-type") or None,
    )


@app.post("/mcp")
async def mcp_post(request: Request) -> Response:
    return await _proxy(request)


@app.get("/mcp")
async def mcp_get(request: Request) -> Response:
    return await _proxy(request)


@app.delete("/mcp")
async def mcp_delete(request: Request) -> Response:
    return await _proxy(request)
