# Managed Device Realtime Authentication

This is an authentication integration, not a separate bridge service.

## Architecture

`flutter_utils/hooks.py` registers `flutter_utils.auth.validate` in `auth_hooks`.
The hook parses authorization headers and delegates managed-device verification
to `flutter_utils.authentication.device`. Firebase verification remains in
`flutter_utils.firebase_auth`.

Verified identities are stored only in `frappe.local.flutter_utils_identity` and
contain the user, authentication method, and optional credential name—not secrets.
`get_authenticated_identity()` returns this context only while its user matches
the current session. Cookie sessions and legacy User tokens do not populate it.

Native HTTP `token` authentication remains handled by Frappe first. For a managed
authorization source, the hook additionally checks device/user status and records
the identity without replacing the native authenticated user. Realtime uses the
same device verifier through the `FlutterDevice` scheme.

Logout and push APIs retain their HTTP `token` plus source-header contract.
Their shared `require_current_device_credential()` helper reverifies ownership and
status rather than trusting a cached identity after revocation.

## Transport contract

No changes to Frappe core or its Socket.IO middleware are required.

For managed credentials, Socket.IO clients send:

```http
Authorization: FlutterDevice <api_key>:<api_secret>
```

Stock Frappe Socket.IO forwards Authorization during authentication and subsequent
permission checks. The registered `flutter_utils.auth.validate` hook validates
this scheme against enabled Flutter Device Credential records, checks the user's
enabled status, and compares the secret before setting the request user. Frappe's
normal request permissions and IP restrictions still apply.

Do not send credentials in query strings. Browser clients must use a transport
that supports the Authorization header (polling-first Socket.IO), or a trusted
server-side proxy. Browser WebSocket handshakes cannot attach arbitrary headers.
Verify the deployed transport and CORS configuration before rollout.

Ordinary HTTP requests, including logout_device, continue using `Authorization:
token ...` with `Frappe-Authorization-Source: Flutter Device Credential`. Legacy
User credentials retain the token scheme for realtime as well.

Deploy flutter_utils before the updated realtime clients and restart Python
workers and the Socket.IO process. Frappe loads the app's `realtime/handlers.js`
at process startup/first connection, so restarting only Python is insufficient.
If an earlier framework middleware patch was running, revert it and restart
Socket.IO too.

## Immediate server-enforced revocation

The app-level handler binds managed sockets to a hash of their API key and login
generation in their site namespace. A private Redis channel,
`flutter_utils:realtime_control`, carries committed revocation messages to each
Socket.IO process. The handler disconnects matching sockets server-side and
removes them from rooms; a client cannot opt out or trigger this channel by
emitting a Socket.IO event. Raw API keys and secrets are not published.

Covered paths:

- Device logout, device-limit eviction, and background limit pruning.
- Administrator disabling or deleting a device credential via ORM.
- Disabling a User via ORM (all their managed sockets on that site).
- Login secret rotation, including admin ORM secret changes. Rotation targets
  the old generation without disconnecting sockets using the new secret.

Notifications run only after database commit. Rollback does not disconnect a
still-valid device. Low-level writes outside these lifecycle helpers/ORM hooks
do not produce an immediate notification; use the supported lifecycle methods.

The handler subscribes before revalidating a new connection. While this finishes,
room joins and inbound packets are held, preventing a startup revocation gap.
Control-channel loss fails closed by disconnecting managed sockets. Cookie and
legacy User-token sockets are not affected.

Redis Pub/Sub is not durable: if a notification is lost, an authenticated backend
recheck every 30 seconds catches stale connections, with a 10-second request
timeout. Thus immediate disconnection is event-driven under normal operation,
not a hard delivery guarantee during infrastructure failure. This costs one
additional backend request per managed socket every 30 seconds.

Clients receive Socket.IO's `io server disconnect`. Treat that as requiring fresh
authentication; do not blindly reconnect with revoked credentials. HTTP and
realtime authorization headers remain unchanged.

### Verification

Run Node tests from `apps/flutter_utils`:

```bash
node --test realtime/registry.test.js realtime/handlers.test.js
```

The handler tests use real Socket.IO server/client transports with controlled
Redis and backend-verification fixtures. Python tests are in
`flutter_utils.tests.test_realtime_disconnection`.

After deployment, additionally verify logout/eviction using the actual site's
Redis, Python web workers, and Socket.IO process. Confirm another device and
another site remain connected.
