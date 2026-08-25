# Groww broker integration

kiwiT integrates with Groww for authenticated, read-only reconciliation. Broker order creation, modification, and cancellation are hard-disabled and are not exposed by the API. The paper ledger remains the only execution target.

## Supported operations

- Account profile and enabled NSE segments
- Holdings and current positions
- Available margin through the internal client
- Quote snapshots and existing-order status through the internal client
- Sanitized errors that never include the access token or Groww response message

The adapter uses only `https://api.groww.in`, API version `1.0`, bounded timeouts, strict symbol/order-ID validation, and the documented Bearer token header.

## Credential setup

An active Groww Trading API subscription is required. Generate an access token in Groww's Trading APIs settings and store it only as `KIWIT_GROWW_ACCESS_TOKEN` in the EC2 environment/GitHub protected environment. Groww's manually generated access token expires daily at 06:00; renewal and revocation remain an operator responsibility.

Do not paste the token into chat, commit it, put it in a URL, or expose it to the browser dashboard. After setting the token, redeploy and call the authenticated `/api/v1/broker/status` and `/api/v1/broker/profile` endpoints. No broker credential is required while paper research continues.

## Gate before any live mutation

Live order routing remains blocked until a separate review establishes regulator/broker compliance, instrument master reconciliation, idempotent order persistence, stale-quote rejection, exchange-hours controls, maximum quantity/notional limits, broker-vs-ledger reconciliation, partial-fill handling, independent kill switches, human approval expiry, and a staged shadow/canary rollout. Enabling a configuration flag alone must never unlock live execution.
