# Flutter Utils

Flutter utility APIs for Frappe – exception handling and email/SMS OTP authentication.

## Features

Realtime transport authentication is documented in
[Managed Device Realtime Authentication](docs/realtime-authentication.md).
Managed sockets are disconnected server-side after device revocation commits;
deployments of the realtime handler require restarting Socket.IO as well as Python.

- **Exception Handler**: Patches Frappe's default exception handler to return structured, human-readable JSON responses for Flutter clients.
- **Email OTP Authentication**: Passwordless login and signup via configurable OTP sent to email.
- **Native Password + 2FA Authentication**: Frappe-managed email/password login with Email, SMS, or OTP App verification, returning browser sessions or managed device credentials.
- **Mobile OTP Authentication**: Passwordless login and signup via 6-digit OTP sent to mobile using Twilio.
- **Firebase Authentication**: Firebase ID-token verification with Frappe session, API credential, and per-request authentication modes.
- **Multi-device Authentication**: Issues independently revocable API credentials per client installation and enforces a configurable per-user device limit.

## API Endpoints

### Authentication

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `flutter_utils.api.auth.login` | Login with email + password |
| POST | `flutter_utils.api.auth.send_otp` | Generic OTP sender for `login` or `signup` using `email` or `mobile` |
| POST | `flutter_utils.api.auth.verify_otp` | Generic OTP verifier for `login` or `signup` |
| POST | `flutter_utils.api.auth.firebase_session_login` | Exchange a Firebase ID token for a Frappe session |
| POST | `flutter_utils.api.auth.firebase_token_login` | Exchange a Firebase ID token for Frappe API credentials |
| POST | `flutter_utils.api.auth.link_firebase_identities` | Link two recently authenticated Firebase identities to one Frappe user |
| POST | `flutter_utils.api.auth.logout_device` | Revoke the managed credential used for the current request |
| GET | `flutter_utils.api.auth.get_session_context` | Return the authenticated browser session and CSRF token |
| POST | `flutter_utils.api.auth.logout_session` | End the current browser session |

### Device Token Authentication

Password login, OTP login/signup verification, and `firebase_token_login` require a stable random installation UUID as `device_id`. `device_name` is optional display metadata. OTP send and password-reset flows do not require device information.

```json
{
  "usr": "user@example.com",
  "pwd": "example-password",
  "device_id": "ceceb8d4-16a7-47b9-baaa-5735b73086f8",
  "device_name": "Chrome on macOS"
}
```

Token login responses include device-specific credentials and their Frappe authorization source:

```json
{
  "api_key": "device-api-key",
  "api_secret": "device-api-secret",
  "authorization_source": "Flutter Device Credential"
}
```

Send both headers on every protected request, including `logout_device`:

```http
Authorization: token <api_key>:<api_secret>
Frappe-Authorization-Source: Flutter Device Credential
```

`Maximum Logged-in Devices` in Flutter Utils Settings defaults to `1`. A login beyond the configured limit revokes the oldest active device credential. Lowering the setting queues background pruning of existing excess credentials. Browser `sid` sessions and `firebase_session_login` remain governed by Frappe's standard session policy.

Firebase identity linking requires fresh ID tokens for both accounts. The endpoint is idempotent, permits
multiple Firebase UIDs to resolve to one Frappe user, and refuses to merge identities that already belong to
different Frappe users. Existing Frappe users require manual business-data reconciliation before their
identity mappings can be combined.

Clients can also authenticate each API request without creating a Frappe session by sending:

```http
Authorization: Firebase <firebase-id-token>
```

The Flutter Utils authentication hook verifies the token and resolves its Firebase identity to the linked
Frappe user for the duration of that request.

```json
{
  "primary_id_token": "<firebase-id-token>",
  "secondary_id_token": "<firebase-id-token>"
}
```

Legacy wrappers still exist for backward compatibility:

- `send_login_otp`
- `verify_login_otp`
- `send_mobile_login_otp`
- `verify_mobile_login_otp`
- `send_signup_otp`
- `verify_signup_otp`
- `send_mobile_signup_otp`
- `verify_mobile_signup_otp`

### Native Password + 2FA Login

Configure **System Settings > Two Factor Authentication** and the applicable roles in Frappe.
Flutter Utils does not override the native method, expiry, enrollment, or attempt tracking.
When 2FA does not apply to the user, password login completes immediately.

POST `/api/method/flutter_utils.api.auth.login`:

```json
{
  "usr": "user@example.com",
  "pwd": "account-password",
  "auth_mode": "session"
}
```

If verification is required, the API's `message` object contains a challenge, not credentials:

```json
{
  "message": {
    "authenticated": false,
    "auth_mode": "session",
    "verification": {"method": "OTP App", "setup": true},
    "tmp_id": "temporary-login-id"
  }
}
```

Display Frappe's verification prompt and submit to the **same endpoint**:

```json
{
  "otp": "012345",
  "tmp_id": "temporary-login-id",
  "auth_mode": "session"
}
```

Keep OTPs as strings to preserve leading zeros. First-time OTP App enrollment uses Frappe's emailed
QR-code link; do not interpret its initial Email prompt as a change in the configured method.
An expired challenge requires starting login again. A password-reset response must be handled before
login can complete. HTTP 200 alone does not mean the user is authenticated.

After successful verification, session mode returns the current user, roles, and CSRF token inside
`message`, with the Frappe session cookie. Browser clients must use `credentials: "include"`, send
`X-Frappe-CSRF-Token` on later writes, restore state through `get_session_context`, and sign out through
`logout_session`.

For device credentials, use `auth_mode: "token"` (the default) and include the same persistent
`device_id` and optional `device_name` on both requests. Credentials are issued only after native login
completes. The existing device-token response and authorization headers are unchanged.

**Migration:** The former `Require Password for Email Login OTP` setting and `password` argument to
`send_otp` have been removed. Move password-login clients from `send_otp`/`verify_otp` to `login`, and
configure native Frappe 2FA before rollout; the removed setting does not enable native 2FA automatically.
Run `bench --site <site> migrate` to sync the settings schema.

Passwordless email/mobile OTP, signup/reset, and Firebase authentication remain separate flows and do
not enforce native Frappe password-login 2FA. Flutter Utils OTP length and templates apply only to
those custom OTP flows, not native 2FA. Existing session/device credentials are not revoked by this
change.

For `flutter_utils.api.auth.login`, `Test Mode` also skips native Email/SMS challenge delivery and
returns the six-digit code as `otp` beside `tmp_id` and `verification`. Clients can prefill the code
and must still submit it with `tmp_id` to complete login. Password checks, native challenge expiry,
and OTP verification remain enforced. Authenticator-app setup/verification and direct Frappe Desk
login are unchanged. Disable `Test Mode` for real two-factor protection; normal mode never returns
the challenge code.

## Twilio Configuration

After `bench migrate`, open `Flutter Utils Settings` from Desk and configure:

- `Maximum Logged-in Devices`
- `Enable Email OTP`
- `Enable Mobile OTP`
- `Test Mode`
- `OTP TTL (Seconds)`
- `SMS Gateway`
- `Twilio Account SID`
- `Twilio Auth Token`
- `Twilio From Number`
- `UltraMsg Base URL`
- `UltraMsg Instance ID`
- `UltraMsg Token`
- `Default Region` as a `Country` dropdown
- `Email Subject Template`
- `Email Body Template`
- `SMS Body Template`
- `Send Test Message`

Mobile numbers are validated and normalized to E.164 format before lookup, OTP delivery, and user creation. If the client sends a number without a `+` prefix, the app uses the selected `Default Region` country and falls back to `System Settings > Country`.

When `Test Mode` is enabled, OTP send APIs do not send email or SMS. They return the generated OTP directly in the JSON response as `otp`, along with `test_mode: 1`.

Templates are also settings-based now. Supported placeholders:

- `{{ app_name }}`
- `{{ otp }}`
- `{{ action }}`
- `{{ expiry_minutes }}`
- `{{ expiry_seconds }}`
- `{{ full_name }}` for SMS templates

Supported SMS gateways:

- `Twilio`
- `UltraMsg`

UltraMsg delivery uses the official endpoint:

- `POST /{instance_id}/messages/chat`
- body params: `token`, `to`, `body`
- default base URL: `https://api.ultramsg.com`

## Integration Test

`Flutter Utils Settings` now includes a Desk test section so you can verify the configured provider directly:

- click `Send Test Message`
- enter the recipient email or mobile number in the prompt

This sends a real message using the currently configured email backend or selected SMS gateway, with a built-in default message. It does not use OTP test mode.

## Generic OTP Usage

Login by email:

```json
{
  "purpose": "login",
  "channel": "email",
  "email": "user@example.com"
}
```

Login by mobile:

```json
{
  "purpose": "login",
  "channel": "mobile",
  "mobile_no": "+919876543210"
}
```

Signup with OTP sent to mobile:

```json
{
  "purpose": "signup",
  "channel": "mobile",
  "full_name": "John Doe",
  "email": "john@example.com",
  "mobile_no": "+919876543210"
}
```

Verify:

```json
{
  "purpose": "signup",
  "channel": "mobile",
  "mobile_no": "+919876543210",
  "otp": "123456",
  "device_id": "ceceb8d4-16a7-47b9-baaa-5735b73086f8",
  "device_name": "Android Device"
}
```

The mobile signup API creates the Frappe `User` with:

- `email` as the user ID
- `mobile_no` populated from the verified number
- Per-device API credentials returned after verification
# Firebase push notifications

Reusable managed-device registration and queued Firebase delivery are documented in
[PUSH_NOTIFICATIONS.md](PUSH_NOTIFICATIONS.md). Push uses the existing Firebase
service account in Flutter Utils Settings and requires Flutter Device Credential authentication.
