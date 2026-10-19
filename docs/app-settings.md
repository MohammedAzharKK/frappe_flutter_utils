# App release settings

`Flutter Utils Settings > App Settings` publishes Android and iOS release state so
Flutter clients can show an update prompt or a maintenance screen **before login**.

The state is **advisory**. No server endpoint is blocked and no credential is
revoked. A misconfigured minimum version or a stuck maintenance toggle can make a
screen appear, but it can never lock users out of the API.

## Configuration

### Enable App Settings

Off by default. While disabled, `managed` is always `false` and every flag is
clear, so existing clients are unaffected.

### Application Identifier

Optional single value for the site. Send it as `app` when calling
`get_app_config`. When it is set and the client's `app` does not match, the
response reports `managed: false` and no flags, so a client that ships several app
identifiers from one codebase is never gated on another app's release state.

### Support Email / Support URL

Optional contact details returned to clients for the update and maintenance
screens. They are published regardless of `managed`, so a client can show support
details even when nothing is gated.

### Android and iOS sections

Each platform carries the same fields, gated independently.

| Field | Purpose |
|---|---|
| `Current Version` | Latest version published to the store. Required while enabled. |
| `Minimum Version` | Oldest version allowed to keep using the app. Optional. |
| `Force Update` | Tell clients below the minimum version to show a non-dismissable screen. |
| `Temporarily Unavailable` | Report the platform as down so clients show a maintenance screen. |
| `Unavailable Message` | Message for that screen. A default message is used when empty. |
| `Play Store URL` / `App Store URL` | Store listing the client opens on the update action. |
| `Release Notes URL` | Optional changelog link for the current version. |
| `Released On` | Optional release date. |

`Force Update` only changes the client experience. Nothing is enforced server-side
either way.

## Endpoint

```http
GET /api/method/flutter_utils.api.app_settings.get_app_config
```

Callable as a guest, because the screens must render before login.

| Argument | Required | Description |
|---|---|---|
| `platform` | yes | `android`, `ios`, or an unmanaged platform such as `web`, `macos`, `windows`, `linux`. |
| `app_version` | no | Version reported by the client, such as `1.2.3` or `1.2.3+45`. |
| `app` | no | Application identifier used to detect a mismatched app. |

```json
{
	"enabled": true,
	"app": "my_app",
	"platform": "android",
	"managed": true,
	"app_version": "1.1.0",
	"current_version": "1.4.2",
	"minimum_version": "1.2.0",
	"update_available": true,
	"update_required": true,
	"force_update": true,
	"unavailable": false,
	"unavailable_message": null,
	"store_url": "https://play.google.com/store/apps/details?id=com.example",
	"release_notes_url": "https://example.com/changelog",
	"released_on": "2026-09-30",
	"support_email": "support@example.com",
	"support_url": "https://example.com/support",
	"checked_at": "2026-10-06 13:52:00.000000"
}
```

`unavailable_message` is only populated while `unavailable` is `true`. It is named
that way rather than `message` because Frappe wraps every response in its own
`message` envelope key, and a nested `message.message` is easy to misread.

Any other platform returns `managed: false` with no version fields, so web and
desktop builds never gate on a mobile version.

## Version comparison

Comparison is numeric per dot-separated part, so `1.10.0` is newer than `1.9.0`
rather than the string ordering a naive comparison would produce.

- Missing parts count as zero: `1.2` equals `1.2.0`.
- A pre-release sorts below its own release: `1.0.0-beta` is older than `1.0.0`.
- Numeric pre-release identifiers compare numerically and rank below
  alphanumeric ones, so `1.0.0-beta.2` is older than `1.0.0-beta.10`.
- A leading `v` is tolerated.
- Build metadata after `+` is ignored, so `1.2.3+45` equals `1.2.3`.

A missing or malformed `app_version` is treated as **unknown**, never as outdated.
`update_required` and `force_update` stay false, which is deliberate: a client that
reports an unparseable version must not be pushed into a blocking screen because of
a parsing quirk. Admin-entered versions are a different case and are rejected by
settings validation.

## Client flow

Call the endpoint before showing the login screen, passing `Platform` from
`dart:io` and the version from `package_info_plus`:

```dart
final config = await Dio().get<Map<String, dynamic>>(
  '/api/method/flutter_utils.api.app_settings.get_app_config',
  queryParameters: {'platform': Platform.isAndroid ? 'android' : 'ios', 'app_version': version},
);

if (config['unavailable'] == true) {
  // Blocking maintenance screen using config['unavailable_message'] and config['support_email'].
} else if (config['update_required'] == true && config['force_update'] == true) {
  // Blocking update screen opening config['store_url'].
} else if (config['update_available'] == true) {
  // Dismissable update prompt.
}
```

Treat every value as advisory and tolerate missing keys. Older clients that do not
call this endpoint keep working unchanged.

## Validation

While `Enable App Settings` is on, saving the settings document rejects a missing or
malformed `Current Version`, a malformed `Minimum Version`, and a `Minimum Version`
newer than `Current Version`. Each message names the platform that failed.