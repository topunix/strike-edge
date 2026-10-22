# TODO: key the inbound rate limiter on the trusted edge address

Status: known issue, risk accepted for the hackathon (decided 2026-10-22). Not a GitHub issue.

## Problem

The inbound limiter (`app/middleware.py`, `client_ip`) keys each client on the **first**
`X-Forwarded-For` entry, as the original spec asked. Behind Render that entry is whatever
the caller sends: the platform appends the real address after it. A caller can therefore
send a different made-up first entry on every request and get a fresh 60 requests per
minute each time.

## Evidence (live site, https://strike-edge.onrender.com, 2026-10-22)

- With a fixed first entry, exactly 60 requests passed and request 61 returned `429` with
  the `{error, detail}` body and a `retry-after` header. `/healthz` and `/static` stayed exempt.
- With a different first entry (a fresh made-up address), requests were allowed immediately
  while the first key was limited.
- 70 requests with no header were never limited. Probably the test traffic left through
  several egress addresses, so each looked like a different client. Not confirmed.
- The traffic path is Cloudflare, then Render, then uvicorn (`server: cloudflare`,
  `x-render-origin-server: uvicorn`).

## Impact

Low. The limiter only protects this service's own CPU and response bandwidth. It does
**not** affect upstream API usage: Strike, Hyperliquid and OKX are called only by the
poller and the history backfill, on a fixed schedule, with their own per-venue token
buckets (at most 50% of each published limit). Extra inbound requests are served from the
in-memory state and the 30 second response cache.

## Fix

1. Confirm the proxy chain and what each hop adds. Temporarily log (or expose on a
   protected debug route) `X-Forwarded-For`, `CF-Connecting-IP` and the socket address for
   a few real requests on Render, and check Render's and Cloudflare's header documentation.
2. Key on the address appended by the trusted edge, not the caller:
   - preferred: `CF-Connecting-IP` if Cloudflare always sets it and Render passes it through;
   - otherwise the correct hop from the **right** of `X-Forwarded-For` (rightmost entry, or
     the Nth from the right once the number of trusted proxies is known). Never the first entry.
3. Fall back to the socket address when the header is absent. Keep `/healthz` and `/static` exempt.
4. Update the README rate-limit wording and `/llms.txt` (both say "first `X-Forwarded-For` entry").

## Tests to add

- A request that sends its own first `X-Forwarded-For` entry cannot change its bucket: two
  requests with different spoofed first entries but the same trusted hop share one budget.
- Different trusted-hop addresses get separate budgets.
- Missing header falls back to the socket address.
- Rewrite `test_inbound_uses_first_x_forwarded_for_entry` in `tests/test_inbound_and_poller.py`,
  which encodes the current first-entry behavior.
