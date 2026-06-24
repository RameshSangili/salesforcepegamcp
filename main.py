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

# tool_name -> {camelCaseParam: original-kebab-param}
# Built from tools/list so we can reverse-map in tools/call.
_param_map: dict[str, dict[str, str]] = {}


def _kebab_to_camel(name: str) -> str:
    parts = name.split("-")
    return parts[0] + "".join(p.capitalize() for p in parts[1:])


def _transform_tools_list(response: dict) -> dict:
    """
    Two transformations applied to every tool schema before sending to Pega:

    1. kebab-case → camelCase rename
       Pega treats hyphens as minus operators, so param names like 'sobject-name'
       break intent generation. We rename them to camelCase and store a reverse
       map so tools/call can convert back before forwarding to Salesforce.

    2. Remove 'body' parameter, set additionalProperties=true
       Salesforce MCP server treats every argument except the object-name param
       as a direct SObject field value (FirstName, LastName, Email, …). When
       'body' appears in the schema, Pega's LLM wraps fields inside it and
       Salesforce then sees 'body' as a column name → "No such column 'body'".
       Removing 'body' from the schema causes the LLM to pass field values
       directly at the top level, matching what Salesforce actually expects.
    """
    tools = response.get("result", {}).get("tools", [])
    for tool in tools:
        tool_name = tool.get("name", "")
        schema = tool.get("inputSchema", {})
        props = schema.get("properties", {})

        # Step 1: rename kebab-case params → camelCase for Pega compatibility
        if any("-" in k for k in props):
            new_props: dict = {}
            _param_map[tool_name] = {}
            for k, v in props.items():
                new_k = _kebab_to_camel(k) if "-" in k else k
                if new_k != k:
                    _param_map[tool_name][new_k] = k
                new_props[new_k] = v
            schema["properties"] = new_props
            props = new_props  # keep local ref in sync
            if "required" in schema:
                schema["required"] = [
                    _kebab_to_camel(r) if "-" in r else r for r in schema["required"]
                ]
            logger.info("Remapped params for tool %s: %s", tool_name, _param_map.get(tool_name))

        # Step 2: remove 'body' so LLM passes SObject fields at the top level
        if "body" in props:
            del props["body"]
            schema["properties"] = props
            schema["additionalProperties"] = True
            req_list = schema.get("required", [])
            if "body" in req_list:
                schema["required"] = [r for r in req_list if r != "body"]
            tool["description"] = (
                tool.get("description", "").rstrip()
                + " Pass SObject field values as direct arguments alongside"
                " sobjectName (e.g. FirstName, LastName, Email, Department,"
                " Phone, AccountId)."
            )
            logger.info("Removed 'body' from schema for tool %s", tool_name)
    return response


def _transform_tools_call(raw: bytes) -> bytes:
    """
    Before forwarding tools/call to Salesforce:
    1. Reverse camelCase→kebab for params renamed in tools/list (sobjectName → sobject-name).
    2. Flatten any 'body' dict to the top level (safety net for cached old schemas).
    """
    try:
        req = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw
    if req.get("method") != "tools/call":
        return raw
    try:
        params = req.get("params") or {}
        tool_name = params.get("name", "")
        mapping = _param_map.get(tool_name, {})
        args_key = "arguments" if "arguments" in params else "input"
        args: dict = dict(params.get(args_key) or {})

        logger.info("tools/call %s raw args: %s", tool_name, list(args.keys()))

        # Step 1: reverse camelCase → kebab-case (e.g. sobjectName → sobject-name)
        args = {mapping.get(k, k): v for k, v in args.items()}

        # Step 2: safety net — flatten any leftover 'body' dict to top level
        body_val = args.get("body")
        if isinstance(body_val, dict):
            del args["body"]
            args.update(body_val)
            logger.info("tools/call %s flattened 'body' key", tool_name)

        req["params"][args_key] = args
        logger.info("tools/call %s final args: %s", tool_name, list(args.keys()))
        return json.dumps(req).encode()
    except Exception as exc:
        logger.error("_transform_tools_call failed (%s), forwarding original body", exc)
        return raw


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
        and k.lower() not in (
            "authorization", "accept", "mcp-session-id",
            "content-type", "salesforce-org-domain",
        )
    }
    headers["Authorization"] = f"Bearer {sf_token}"
    headers["Content-Type"] = "application/json"
    headers["Accept"] = "application/json, text/event-stream"
    # Required by api.salesforce.com global gateway to route to the correct org,
    # especially for write operations (create/update/delete).
    if _token_manager.instance_url:
        org_domain = _token_manager.instance_url.replace("https://", "").rstrip("/")
        headers["Salesforce-Org-Domain"] = org_domain
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

    # Detect JSON-RPC method for response-side transforms
    try:
        parsed = json.loads(body)
        rpc_method = parsed.get("method", "")
        if rpc_method == "tools/call":
            params = parsed.get("params") or {}
            raw_args = params.get("arguments") or params.get("input") or {}
            logger.info(
                "tools/call name=%s args_keys=%s",
                params.get("name"),
                list(raw_args.keys()),
            )
    except (json.JSONDecodeError, AttributeError):
        rpc_method = ""

    # Reverse camelCase→kebab rename before forwarding tools/call to Salesforce
    body = _transform_tools_call(body)

    logger.info(
        "Proxying POST /mcp method=%s (proxy_session=%s sf_session=%s)",
        rpc_method or "-",
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
        if session_data["sf_session_id"] != new_sf_session_id:
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

    if sf_resp.status_code >= 400:
        logger.error(
            "Salesforce error %d (content-type=%s) body=%s",
            sf_resp.status_code, sf_content_type,
            json.dumps(response_items) if response_items else "<empty>",
        )
    else:
        logger.info(
            "Salesforce responded %d (content-type=%s items=%d)",
            sf_resp.status_code, sf_content_type, len(response_items),
        )

    # Rename kebab-case param names to camelCase in tools/list so Pega
    # can create valid intent parameters (hyphens break Pega's reference syntax)
    if rpc_method == "tools/list":
        response_items = [_transform_tools_list(item) for item in response_items]

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
