# Salesforce MCP Proxy

FastAPI app that runs on Google Cloud Run and acts as an MCP proxy between Pega and Salesforce's hosted MCP server.

## Problem

Pega's Connect MCP rule only supports Client Credentials OAuth. Salesforce's hosted MCP server (`api.salesforce.com`) requires a per-user token obtained via Authorization Code flow. This proxy bridges that gap.

```
Pega Connect MCP (Client Credentials token)
    ↓  POST /mcp
FastAPI on Cloud Run   ← this repo
    ↓  injects pre-authorized user token
Salesforce Hosted MCP Server
https://api.salesforce.com/platform/mcp/v1/platform/sobject-all
```

---

## Step 1 — Get a refresh token via Bruno (one-time)

1. Open Bruno and create a new request.
2. Set method to **GET** and URL to:
   ```
   https://dllbp.my.salesforce.com/services/oauth2/authorize
   ```
3. Add query params:
   | Key | Value |
   |---|---|
   | `response_type` | `code` |
   | `client_id` | `3MVG9nSH73I5aFNi1._.oYzqFFlQX7QCSbG7NKSXbytZQQ3gE9A.XzpOme5Luew3GXmNc9fbhZdVLGq_JyN7g` |
   | `redirect_uri` | `https://login.salesforce.com/services/oauth2/success` |
   | `scope` | `api refresh_token` |

4. Open the constructed URL in a browser. Log in as the integration user and click **Allow**.
5. After redirect, copy the `code` value from the URL bar.
6. In Bruno, make a **POST** request to `https://dllbp.my.salesforce.com/services/oauth2/token` with form body:
   | Key | Value |
   |---|---|
   | `grant_type` | `authorization_code` |
   | `client_id` | `3MVG9nSH73I5aFNi1._.oYzqFFlQX7QCSbG7NKSXbytZQQ3gE9A.XzpOme5Luew3GXmNc9fbhZdVLGq_JyN7g` |
   | `client_secret` | *(your consumer secret)* |
   | `code` | *(the code from step 5)* |
   | `redirect_uri` | `https://login.salesforce.com/services/oauth2/success` |

7. Copy `refresh_token` from the JSON response. **Store it securely — this is the value for `SALESFORCE_REFRESH_TOKEN`.**

---

## Step 2 — Local development

```bash
cp .env.example .env
# Fill in SALESFORCE_CLIENT_SECRET and SALESFORCE_REFRESH_TOKEN in .env

pip install -r requirements.txt
uvicorn main:app --reload --port 8080
```

Test the health check:
```bash
curl http://localhost:8080/health
```

Test the proxy (Pega-style call):
```bash
curl -X POST http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <PEGA_EXPECTED_TOKEN>" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

---

## Step 3 — Build and deploy to Cloud Run

```bash
PROJECT_ID=your-gcp-project-id
REGION=us-central1
SERVICE=salesforce-mcp-proxy

# Build and push
gcloud builds submit --tag gcr.io/$PROJECT_ID/$SERVICE

# Deploy
gcloud run deploy $SERVICE \
  --image gcr.io/$PROJECT_ID/$SERVICE \
  --platform managed \
  --region $REGION \
  --port 8080 \
  --allow-unauthenticated \
  --set-env-vars "SALESFORCE_CLIENT_SECRET=<secret>,SALESFORCE_REFRESH_TOKEN=<token>,PEGA_EXPECTED_TOKEN=<shared-secret>"
```

> **Tip:** use `--set-secrets` instead of `--set-env-vars` for production to pull values from Secret Manager:
> ```bash
> --set-secrets "SALESFORCE_CLIENT_SECRET=sf-client-secret:latest,SALESFORCE_REFRESH_TOKEN=sf-refresh-token:latest"
> ```

After deploy, note the service URL (e.g. `https://salesforce-mcp-proxy-abc123-uc.a.run.app`).

---

## Step 4 — Configure Pega

1. Open the **TestMCP** Connect MCP rule in Pega.
2. Set the **MCP Server URL** to:
   ```
   https://salesforce-mcp-proxy-abc123-uc.a.run.app/mcp
   ```
3. Leave the existing **Client Credentials** auth profile unchanged — the proxy strips Pega's token and injects the Salesforce user token automatically.
4. If you set `PEGA_EXPECTED_TOKEN`, configure Pega's auth profile to send that value as the Bearer token.

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `SALESFORCE_CLIENT_SECRET` | Yes | Consumer secret of the PegaIntegration Connected App |
| `SALESFORCE_REFRESH_TOKEN` | Yes | Refresh token from one-time Authorization Code flow |
| `PEGA_EXPECTED_TOKEN` | No | Shared secret Pega sends; proxy returns 401 if it doesn't match |
| `SALESFORCE_MCP_URL` | No | Override default Salesforce MCP endpoint |

---

## Token refresh behavior

- On startup the proxy exchanges the refresh token for an access token.
- Before each request the proxy checks if the token expires within 5 minutes; if so it refreshes automatically.
- Salesforce access tokens expire after 2 hours by default. The proxy handles this transparently with no restarts needed.
- Cloud Run instances are stateless — each new instance re-fetches a token on cold start.

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Health check (Cloud Run readiness probe) |
| `POST` | `/mcp` | MCP JSON-RPC requests from Pega |
| `GET` | `/mcp` | MCP SSE stream establishment |
| `DELETE` | `/mcp` | MCP session termination |
