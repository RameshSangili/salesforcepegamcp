import asyncio
import json
import logging
import os
import uuid
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

# proxy_session_id -> {"queue": asyncio.Queue, "sf_session_id": str | None}
_sessions: dict[str, dict] = {}


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


def _upstream_headers(
    request: Request, sf_token: str, sf_session_id: str | None = None
) -> dict[str, str]:
    headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _HOP_BY_HOP
        and k.lower() not in ("authorization", "accept", "mcp-session-id")
    }
    headers["Authorization"] = f"Bearer {sf_token}"
    headers["Accept"] = "application/json, text/event-stream"
    if sf_session_id:
        headers["Mcp-Session-Id"] = sf_session_id
    return headers


def _check_pega_auth(request: Request) -> bool:
    if not PEGA_EXPECTED_TOKEN:
        return True
    auth = request.headers.get("Authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    return token == PEGA_EXPECTED_TOKEN


@app.get("/mcp")
async def mcp_get(request: Request) -> Response:
    """
    MCP HTTP+SSE transport (Pega 25.1.2):
    1. Pega opens this SSE channel.
    2. We send an 'endpoint' event with the POST URL (including session ID).
    3. Pega POSTs all JSON-RPC requests to that URL.
    4. We forward each POST to Salesforce and push the response back here
       as an SSE 'message' event.
    5. Pega reads responses from this stream.
    """
    if not _check_pega_auth(request):
        return Response(status_code=401, content=b"Unauthorized")

    proxy_session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    _sessions[proxy_session_id] = {"queue": queue, "sf_session_id": None}

    proto = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("host", "")
    endpoint_url = f"{proto}://{host}/mcp?sessionId={proxy_session_id}"

    logger.info("SSE opened proxy_session=%s endpoint=%s", proxy_session_id, endpoint_url)

    async def sse_stream() -> AsyncIterator[bytes]:
        try:
            # Tell Pega where to POST
            yield f"event: endpoint\ndata: {endpoint_url}\n\n".encode()
            # Stream JSON-RPC responses back as 'message' events
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15.0)
                    if item is None:  # sentinel: close stream
                        break
                    yield f"event: message\ndata: {json.dumps(item)}\n\n".encode()
                except asyncio.TimeoutError:
                    yield b": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            _sessions.pop(proxy_session_id, None)
            logger.info("SSE closed proxy_session=%s", proxy_session_id)

    return StreamingResponse(
        sse_stream(),
        status_code=200,
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        media_type="text/event-stream",
    )


@app.post("/mcp")
async def mcp_post(request: Request) -> Response:
    if not _check_pega_auth(request):
        return Response(status_code=401, content=b"Unauthorized")

    # Identify session from URL query param set in our endpoint event.
    # Pega 25.1.2 ignores the ?sessionId= and always POSTs to the base /mcp URL,
    # so fall back to the most recently opened SSE session when no param is present.
    proxy_session_id = request.query_params.get("sessionId")
    session_data = _sessions.get(proxy_session_id) if proxy_session_id else None
    if session_data is None and _sessions:
        latest_key = next(reversed(_sessions))
        session_data = _sessions[latest_key]
        logger.info("No sessionId in POST — routing to latest SSE session %s", latest_key)
    sf_session_id = session_data["sf_session_id"] if session_data else None

    sf_token = await _token_manager.get_access_token()
    req_headers = _upstream_headers(request, sf_token, sf_session_id)
    body = await request.body()

    logger.info(
        "Proxying POST /mcp (proxy_session=%s sf_session=%s)",
        proxy_session_id or "-",
        sf_session_id or "-",
    )

    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
        follow_redirects=True,
    )
    sf_req = client.build_request(
        "POST", SALESFORCE_MCP_URL, headers=req_headers, content=body
    )

    try:
        sf_resp = await client.send(sf_req, stream=True)
    except httpx.RequestError as exc:
        await client.aclose()
        logger.error("Upstream request failed: %s", exc)
        return Response(status_code=502, content=b"Bad Gateway")

    sf_content_type = sf_resp.headers.get("content-type", "")
    new_sf_session_id = sf_resp.headers.get("mcp-session-id")

    # Store Salesforce session ID so subsequent POSTs include it
    if new_sf_session_id and session_data is not None:
        session_data["sf_session_id"] = new_sf_session_id
        logger.info("SF session stored %s → %s", proxy_session_id, new_sf_session_id)

    # Parse Salesforce response (JSON or SSE)
    response_items: list[dict] = []
    try:
        if "text/event-stream" in sf_content_type:
            async for line in sf_resp.aiter_lines():
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if payload and payload != "[DONE]":
                        try:
                            response_items.append(json.loads(payload))
                        except json.JSONDecodeError:
                            pass
        else:
            raw = await sf_resp.aread()
            if raw:
                try:
                    response_items.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
    finally:
        await sf_resp.aclose()
        await client.aclose()

    logger.info(
        "Salesforce responded %d (content-type=%s items=%d)",
        sf_resp.status_code, sf_content_type, len(response_items),
    )

    # HTTP+SSE transport: push response into SSE stream, return 202 to Pega
    if session_data is not None:
        for item in response_items:
            await session_data["queue"].put(item)
        resp_headers = {}
        if new_sf_session_id:
            resp_headers["Mcp-Session-Id"] = new_sf_session_id
        return Response(status_code=202, headers=resp_headers, content=b"")

    # Fallback (no session): return inline
    if response_items:
        return Response(
            content=json.dumps(response_items[-1]).encode(),
            status_code=sf_resp.status_code,
            media_type="application/json",
        )
    return Response(status_code=sf_resp.status_code, content=b"")


@app.delete("/mcp")
async def mcp_delete(request: Request) -> Response:
    if not _check_pega_auth(request):
        return Response(status_code=401, content=b"Unauthorized")
    proxy_session_id = request.query_params.get("sessionId")
    if proxy_session_id:
        session = _sessions.pop(proxy_session_id, None)
        if session:
            await session["queue"].put(None)  # signal SSE stream to close
    return Response(status_code=200, content=b"")
