from typing import Any

import frappe
from frappe.rate_limiter import rate_limit

from flutter_utils.api.auth import get_flutter_utils_settings
from flutter_utils.app_settings import build_app_config


@frappe.whitelist(allow_guest=True, methods=["GET"])
@rate_limit(limit=120, seconds=60, methods="GET")
def get_app_config(platform: str, app_version: str | None = None, app: str | None = None) -> dict[str, Any]:
	"""
	Return Android or iOS release state so the app can show an update or
	unavailable screen before login.

	This endpoint is advisory. It never rejects a request and never invalidates
	existing credentials, so a misconfigured minimum version or a stuck
	maintenance toggle cannot lock users out of the API.

	:param platform: `android`, `ios`, or an unmanaged platform such as `web`.
	:param app_version: Version reported by the client, such as `1.2.3` or
	        `1.2.3+45`. Optional; a missing or malformed value is treated as
	        unknown and never reported as outdated.
	:param app: Optional application identifier. When it does not match the
	        configured `Application Identifier`, the response reports
	        `managed: false` so a multi-app client is never gated on another
	        app's release state.
	:returns: Release state for the requested platform.
	"""
	return build_app_config(get_flutter_utils_settings(), platform, app_version, app)
