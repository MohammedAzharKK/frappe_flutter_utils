# Firebase certificate caching

## Purpose and verification boundary

`firebase_certificates.py` caches Google's **public signing certificates**, not ID tokens,
user information, or successful authentication results. The cache is site-scoped in Redis
and survives Python worker restarts. All workers on that site reuse it.

`firebase_tokens.py` uses the public `google.auth.jwt` verifier instead of patching private
Firebase Admin SDK transports. It enforces RS256, a nonempty key ID, the configured project
audience, the exact Firebase issuer, a nonempty subject of at most 128 characters, signature,
issued-at, and expiration. Firebase Admin's public `get_user` API retains the optional
disabled-user/revocation check, including its issued-at comparison. Existing provider,
verified-contact, Frappe user, and device-credential policies are unchanged.

Unsigned Auth Emulator tokens are deliberately not accepted by this verification path.

## Freshness and failure behavior

- Fetch only the fixed Google HTTPS certificate endpoint; validate TLS and reject redirects.
- Respect `Cache-Control: max-age` minus `Age` and download duration, with an additional
  maximum cache lifetime of 24 hours. No stale-cache authentication fallback.
- Refresh every five minutes when fewer than 15 minutes of freshness remain. Skip sites
  where Firebase authentication is disabled.
- Coordinate refreshes with a site-scoped Redis lock and a 60-second attempt cooldown.
- An unknown key ID can trigger a bounded refresh for certificate rotation. If fresh
  certificates still do not contain that key, reject the token.
- A failed refresh preserves still-fresh certificates **without extending their expiry**.
- No fresh keys means authentication fails closed with the existing `firebase_unavailable`
  response. A malformed token or unverifiable signature never becomes authenticated.
- Warm verification makes no Google request, unless optional revocation checking is enabled.

Cold-cache/key-rotation fetching must be synchronous: an identity cannot be trusted before
the signature is verified. The first cold request may fetch; concurrent requests without
usable keys fail temporarily rather than queue behind an unbounded refresh. Prewarm before
accepting traffic to avoid this initial dependency. Refreshes normally run in background.

HTTP connect/read limits are 3/5 seconds as a fallback, not a performance fix or guaranteed
wall-clock deadline: DNS resolution and multiple address attempts can take longer.

## Rollout

Deploy **flutter_utils**, not just the consuming app, using the normal authorized pipeline.
Install the updated Python dependencies and run the site's migration so the new scheduler
hook is registered. Ensure scheduler/background workers are enabled and running.

For an authorized development rollout, the relevant site operations are:

```sh
bench --site sync-backend.coreaxissolutions.in migrate
bench --site sync-backend.coreaxissolutions.in execute flutter_utils.firebase_certificates.refresh_firebase_certificates
```

The execute command is a one-time prewarm. Restart web/scheduler/background processes through
the normal deployment lifecycle. Existing Redis certificate data survives those restarts.
No Flutter release or site configuration/DNS/IPv6 change is required for this improvement.

## Verification and diagnostics

Offline regression tests, from the bench root:

```sh
env/bin/python -m unittest flutter_utils.tests.test_firebase_certificate_cache -v
```

On a test-enabled Frappe site, also run `flutter_utils.tests.test_firebase_auth` and
`flutter_utils.tests.test_authentication_context`. Do not enable destructive database tests
on a live site merely for deployment verification.

`logs/firebase_auth.log` and the site's corresponding log contain JSON timing records for
certificate refresh and token verification. Native token exchange additionally records
`verify_identity`, `resolve_user`, `issue_credentials`, and `login_total`, correlated by a
random `attempt_id`. No tokens, credentials, account identifiers, or raw remote errors
are included. Generic outage errors retain the established API response contract.

After deployment, test Google sign-in followed by an authenticated API read, repeat across
workers, and confirm warm token verification does not fetch certificates. Verify the
scheduled refresh actually executes before relying on it. Offline tests do not establish
live Google/Firebase configuration or identify the underlying intermittent network fault;
DNS/TCP/TLS-level tracing during a stall remains a separate infrastructure investigation.
