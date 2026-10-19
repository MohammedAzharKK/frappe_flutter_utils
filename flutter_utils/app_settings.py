"""Release-state comparison for Flutter clients.

Version comparison and app-config assembly live here so the public endpoint stays
thin and every surface reports identical state. Nothing in this module enforces
anything server-side; the settings only describe what clients should present.
"""

from __future__ import annotations

import re
from typing import Any

import frappe
from frappe import _

# Platforms with configurable release state. Other platforms are reported as
# unmanaged so clients never gate a web or desktop build on a mobile version.
APP_PLATFORMS = ("android", "ios")
UNMANAGED_PLATFORMS = ("web", "macos", "windows", "linux")

# Per-platform settings suffixes. A platform is treated as configured once any of
# these is set, which lets a single-platform deployment stay valid.
PLATFORM_APP_FIELDS = (
	"store_url",
	"current_version",
	"minimum_version",
	"force_update",
	"unavailable",
	"unavailable_message",
	"release_notes_url",
	"released_on",
)

DEFAULT_UNAVAILABLE_MESSAGE = "This app is temporarily unavailable. Please try again later."

# Accepts the shapes real clients report: "1.2", "1.2.3", "v1.2.3", "1.2.3-beta.1",
# and package_info_plus build suffixes such as "1.2.3+45". Build metadata is ignored
# during comparison, and a leading "v" is tolerated.
VERSION_PATTERN = re.compile(
	r"^v?(?P<release>\d+(?:\.\d+)*)"
	r"(?:-(?P<pre>[0-9A-Za-z][0-9A-Za-z.\-]*))?"
	r"(?:\+[0-9A-Za-z][0-9A-Za-z.\-]*)?$"
)

_NUMERIC_IDENTIFIER = re.compile(r"^\d+$")


def normalize_platform(platform: str | None) -> str:
	"""Return a lowercase platform or raise for unknown values."""
	normalized = platform.strip().lower() if isinstance(platform, str) else ""
	if not normalized:
		frappe.throw(_("Platform is required."), frappe.ValidationError)

	if normalized not in APP_PLATFORMS + UNMANAGED_PLATFORMS:
		supported = ", ".join(APP_PLATFORMS + UNMANAGED_PLATFORMS)
		frappe.throw(
			_("Unsupported platform {0}. Supported platforms are {1}.").format(normalized, supported),
			frappe.ValidationError,
		)
	return normalized


def normalize_version(value: str | None) -> str:
	"""Return a trimmed version string without a leading "v" or build metadata."""
	if not isinstance(value, str):
		return ""
	trimmed = value.strip()
	if not trimmed:
		return ""

	match = VERSION_PATTERN.match(trimmed)
	if not match:
		return ""
	if match.group("pre"):
		return f"{match.group('release')}-{match.group('pre')}"
	return match.group("release")


def is_valid_version(value: str | None) -> bool:
	return bool(normalize_version(value))


def parse_version(value: str | None) -> tuple[tuple[int, ...], tuple[str, ...]] | None:
	"""Split a version into numeric parts and pre-release identifiers.

	Returns None when the value is missing or malformed, so callers can treat an
	unparseable client version as unknown rather than as outdated.
	"""
	normalized = normalize_version(value)
	if not normalized:
		return None

	release, _, pre_release = normalized.partition("-")
	numbers = tuple(int(part) for part in release.split("."))
	identifiers = tuple(part for part in pre_release.split(".") if part) if pre_release else ()
	return numbers, identifiers


def compare_versions(left: str | None, right: str | None) -> int:
	"""Compare two versions, returning -1, 0, or 1.

	Missing parts are treated as zero, so "1.2" equals "1.2.0". A pre-release
	sorts below its own release, so "1.0.0-beta" is older than "1.0.0".
	"""
	left_parsed = parse_version(left)
	right_parsed = parse_version(right)
	if left_parsed is None or right_parsed is None:
		raise ValueError(f"Cannot compare malformed versions: {left!r}, {right!r}")

	return _compare_release(left_parsed, right_parsed)


def _compare_release(
	left: tuple[tuple[int, ...], tuple[str, ...]],
	right: tuple[tuple[int, ...], tuple[str, ...]],
) -> int:
	left_numbers, left_pre = left
	right_numbers, right_pre = right

	length = max(len(left_numbers), len(right_numbers))
	left_padded = left_numbers + (0,) * (length - len(left_numbers))
	right_padded = right_numbers + (0,) * (length - len(right_numbers))
	if left_padded != right_padded:
		return -1 if left_padded < right_padded else 1

	return _compare_pre_release(left_pre, right_pre)


def _compare_pre_release(left: tuple[str, ...], right: tuple[str, ...]) -> int:
	# A release without a pre-release outranks the same release with one.
	if not left and not right:
		return 0
	if not left:
		return 1
	if not right:
		return -1

	for left_part, right_part in zip(left, right, strict=False):
		comparison = _compare_identifier(left_part, right_part)
		if comparison:
			return comparison

	if len(left) == len(right):
		return 0
	return -1 if len(left) < len(right) else 1


def _compare_identifier(left: str, right: str) -> int:
	left_is_numeric = bool(_NUMERIC_IDENTIFIER.match(left))
	right_is_numeric = bool(_NUMERIC_IDENTIFIER.match(right))

	if left_is_numeric and right_is_numeric:
		left_number, right_number = int(left), int(right)
		if left_number != right_number:
			return -1 if left_number < right_number else 1
		return 0

	# Numeric identifiers always have lower precedence than alphanumeric ones.
	if left_is_numeric != right_is_numeric:
		return -1 if left_is_numeric else 1

	if left == right:
		return 0
	return -1 if left < right else 1


def build_app_config(
	settings: Any,
	platform: str,
	app_version: str | None = None,
	app: str | None = None,
) -> dict[str, Any]:
	"""Build the public release-state payload for one client platform.

	`settings` is the Flutter Utils Settings document. Flags stay false whenever
	the data needed to decide is missing, so a malformed client version or an
	unconfigured minimum never pushes a user into an update or maintenance screen.
	"""
	normalized_platform = normalize_platform(platform)
	enabled = bool(getattr(settings, "enable_app_settings", 0))
	managed = enabled and normalized_platform in APP_PLATFORMS

	config: dict[str, Any] = {
		"enabled": enabled,
		"app": _as_str(getattr(settings, "app_identifier", "")),
		"platform": normalized_platform,
		"managed": managed,
		"app_version": normalize_version(app_version),
		"current_version": None,
		"minimum_version": None,
		"update_available": False,
		"update_required": False,
		"force_update": False,
		"unavailable": False,
		# Named to avoid colliding with Frappe's own "message" response envelope key.
		"unavailable_message": None,
		"store_url": _as_str(getattr(settings, f"{normalized_platform}_store_url", "")),
		"release_notes_url": _as_str(getattr(settings, f"{normalized_platform}_release_notes_url", "")),
		"released_on": _as_date_string(getattr(settings, f"{normalized_platform}_released_on", None)),
		"support_email": _as_str(getattr(settings, "app_support_email", "")),
		"support_url": _as_str(getattr(settings, "app_support_url", "")),
		"checked_at": frappe.utils.now_datetime(),
	}

	if not managed:
		return config

	if isinstance(app, str) and app.strip() and config["app"] and app.strip() != config["app"]:
		# This settings document describes a different application; never gate on it.
		config["managed"] = False
		return config

	current_version = normalize_version(getattr(settings, f"{normalized_platform}_current_version", ""))
	minimum_version = normalize_version(getattr(settings, f"{normalized_platform}_minimum_version", ""))
	config["current_version"] = current_version or None
	config["minimum_version"] = minimum_version or None

	unavailable = bool(getattr(settings, f"{normalized_platform}_unavailable", 0))
	if unavailable:
		config["unavailable"] = True
		config["unavailable_message"] = (
			_as_str(getattr(settings, f"{normalized_platform}_unavailable_message", ""))
			or DEFAULT_UNAVAILABLE_MESSAGE
		)

	if (
		config["app_version"]
		and current_version
		and compare_versions(config["app_version"], current_version) < 0
	):
		config["update_available"] = True

	if (
		config["app_version"]
		and minimum_version
		and compare_versions(config["app_version"], minimum_version) < 0
	):
		config["update_required"] = True
		config["update_available"] = True
		config["force_update"] = bool(getattr(settings, f"{normalized_platform}_force_update", 0))

	return config


def _as_str(value: Any) -> str:
	return value.strip() if isinstance(value, str) else ""


def _as_date_string(value: Any) -> str | None:
	if not value:
		return None
	return str(value)[:10]
