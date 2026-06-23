import asyncio
import json
from pathlib import Path

import httpx
from dotenv import load_dotenv

from token_manager import TokenManager

_here = Path(__file__).parent
_env_file = _here / ".env" if (_here / ".env").exists() else _here / ".env.example"
print(f"Loading env from: {_env_file}")
load_dotenv(_env_file)

MCP_URL = "https://api.salesforce.com/platform/mcp/v1/platform/sobject-all"


def _rpc(method: str, params: dict, req_id: int) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}


def _notification(method: str) -> dict:
    return {"jsonrpc": "2.0", "method": method}


def _parse_response(response: httpx.Response) -> dict | list:
    if not response.content:
        return {}
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        results = []
        for line in response.text.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    results.append(json.loads(payload))
        return results
    return response.json()


async def mcp_post(
    client: httpx.AsyncClient, headers: dict, body: dict
) -> tuple[dict | list, httpx.Headers]:
    response = await client.post(MCP_URL, headers=headers, json=body)
    if response.status_code not in (200, 202):
        print(f"  HTTP {response.status_code}: {response.text[:500]}")
        print("  Response headers:")
        for k, v in response.headers.items():
            print(f"    {k}: {v}")
        return {}, response.headers
    return _parse_response(response), response.headers


async def main() -> None:
    print("Getting Salesforce access token...")
    tm = TokenManager()
    await tm.initialize()
    token = await tm.get_access_token()
    instance_url = tm.instance_url
    print(f"Token:        {token[:20]}...{token[-10:]}")
    print(f"Instance URL: {instance_url}")
    print(f"Scopes:       {tm.token_scope}\n")
    await tm.close()

    # Extract the org My Domain subdomain (e.g. "dllbp" from "https://dllbp.my.salesforce.com")
    org_domain = instance_url.replace("https://", "").rstrip("/")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
        "Salesforce-Org-Domain": org_domain,   # routing hint for api.salesforce.com gateway
    }

    print(f"Calling: {MCP_URL}")
    print(f"Org domain header: {org_domain}\n")

    async with httpx.AsyncClient(verify=False, timeout=30.0) as client:

        # Step 1: initialize
        print("Step 1 — initialize")
        init_body = _rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "pega-proxy-test", "version": "1.0.0"},
        }, req_id=1)
        result, init_resp_headers = await mcp_post(client, headers, init_body)
        print(json.dumps(result, indent=2))

        if not result:
            return

        # Session ID comes back in the response headers of initialize, not the body
        session_id = (
            init_resp_headers.get("mcp-session-id")
            or init_resp_headers.get("Mcp-Session-Id")
        )
        if session_id:
            headers["Mcp-Session-Id"] = session_id
            print(f"\nSession ID: {session_id}")
        else:
            print("\nWARN: No Mcp-Session-Id in initialize response headers")

        # Step 2: initialized notification
        await mcp_post(client, headers, _notification("notifications/initialized"))

        # Step 3: tools/list
        print("\nStep 2 — tools/list")
        tools_result, _ = await mcp_post(client, headers, _rpc("tools/list", {}, req_id=2))
        print(json.dumps(tools_result, indent=2))

        tools = []
        if isinstance(tools_result, dict):
            tools = tools_result.get("result", {}).get("tools", [])
        elif isinstance(tools_result, list):
            for item in tools_result:
                tools.extend(item.get("result", {}).get("tools", []))

        if not tools:
            print("\nNo tools returned.")
            return

        print(f"\nAvailable tools: {[t['name'] for t in tools]}")

        # Step 4: call soqlQuery to fetch Accounts
        print("\nStep 3 — calling soqlQuery: SELECT Id, Name, Type, Industry FROM Account LIMIT 10")
        call_result, _ = await mcp_post(
            client, headers,
            _rpc("tools/call", {
                "name": "soqlQuery",
                "arguments": {"q": "SELECT Id, Name, Type, Industry FROM Account LIMIT 10"},
            }, req_id=3)
        )
        print(json.dumps(call_result, indent=2))


asyncio.run(main())
