from dataclasses import dataclass

import frappe


@dataclass(frozen=True)
class Authorization:
	scheme: str
	credentials: str


def parse_authorization_header() -> Authorization:
	scheme, _, credentials = frappe.get_request_header("Authorization", "").partition(" ")
	return Authorization(scheme=scheme.lower(), credentials=credentials)


def parse_device_credentials(credentials: str) -> tuple[str, str]:
	api_key, separator, api_secret = credentials.partition(":")
	if not separator or not api_key or not api_secret or ":" in api_secret:
		raise frappe.AuthenticationError
	return api_key, api_secret
