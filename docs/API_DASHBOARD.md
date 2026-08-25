# kiwiT API and Dashboard

The FastAPI control plane exposes paper-account status, account halt/resume controls, evidence retrieval, and sanitized LangGraph checkpoint status. It exposes no endpoint that creates or submits a trade.

## Start locally

Set a random API key of at least 24 characters in `.env` as `KIWIT_API_KEY`, then:

```bash
set -a; source .env; set +a
pip install -e '.[api,production,workflow]'
python scripts/run_api.py
```

Open `http://127.0.0.1:8000/dashboard` and enter the API key. The dashboard keeps it in page memory only. Binding defaults to loopback; do not expose Uvicorn directly to the internet.

## Security boundary

- All `/api/v1` endpoints require `X-KIWIT-API-Key` with constant-time comparison.
- Missing or weak server configuration fails with HTTP 503.
- Responses disable caching, framing, MIME sniffing, and cross-origin asset loading.
- OpenAPI and interactive documentation are disabled.
- Workflow responses expose only status, proposal ID, fill presence, and checkpoint ID.
- Production internet access requires a TLS reverse proxy, identity-aware authentication, rate limiting, request-size limits, and secret rotation.
- The API contains no live-order or trade-creation endpoint.
- Production evidence retrieval reads the indexed Neon knowledge tables; SQLite remains a local research adapter only.
