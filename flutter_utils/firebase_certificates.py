"""Shared, freshness-bounded cache of Google's *public* Firebase signing certificates.

No tokens or verification results are cached. Ordinary warm verification is local;
the scheduler refreshes before expiry. A cold cache still requires a verified HTTPS
fetch before authentication can proceed. Never serve expired cached certificates.
"""

import json
import re
import time
from typing import Any

import frappe
import requests
from cryptography import x509

CERTIFICATE_URL = "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"
CACHE_KEY = "flutter_utils:firebase:certificates:v1"
ATTEMPT_KEY = f"{CACHE_KEY}:attempt"
REFRESH_AHEAD_SECONDS = 900
REFRESH_COOLDOWN_SECONDS = 60


class CertificateUnavailableError(Exception):
	"""No fresh signing certificates are available. Authentication must fail closed."""


def _cached() -> dict[str, Any] | None:
	entry = frappe.cache.get_value(CACHE_KEY, use_local_cache=False)
	if isinstance(entry, dict) and entry.get("expires_at", 0) > time.time():
		return entry
	return None


def get_certificates(key_id: str) -> dict[str, str]:
	entry = _cached()
	if entry and key_id in entry["certificates"]:
		return entry["certificates"]
	# An unknown key can be a legitimate rotation. Coalesce/limit refreshes so
	# arbitrary attacker-selected key IDs cannot cause an unbounded Google fetch.
	entry = _refresh()
	if entry is None:
		raise CertificateUnavailableError("Firebase signing certificates are unavailable.")
	return entry["certificates"]


def _refresh() -> dict[str, Any] | None:
	cache = frappe.cache
	# Use the site's Redis key namespace for both the cache and its lock.
	lock = cache.lock(cache.make_key(f"{CACHE_KEY}:lock"), timeout=60, blocking_timeout=0)
	if not lock.acquire(blocking=False):
		return _cached()
	try:
		if cache.get_value(ATTEMPT_KEY, use_local_cache=False):
			return _cached()
		cache.set_value(ATTEMPT_KEY, True, expires_in_sec=REFRESH_COOLDOWN_SECONDS)
		started = time.monotonic()
		try:
			# Fixed trusted endpoint, TLS validation on, and no redirects. This is
			# synchronous only on a cold cache/key rotation: verification cannot
			# safely succeed before the required signing certificate is available.
			response = requests.get(CERTIFICATE_URL, timeout=(3, 5), allow_redirects=False)
			try:
				response.raise_for_status()
				if response.status_code != 200:
					raise ValueError("Unexpected certificate response status.")
				certificates = response.json()
				if not isinstance(certificates, dict) or not certificates:
					raise ValueError("Invalid certificate response.")
				for key, certificate in certificates.items():
					if not isinstance(key, str) or not isinstance(certificate, str):
						raise ValueError("Invalid certificate response.")
					x509.load_pem_x509_certificate(certificate.encode("ascii"))
				control = response.headers.get("Cache-Control", "")
				match = re.search(r"(?:^|,)\s*max-age=(\d+)(?:\s*,|\s*$)", control, re.I)
				if not match or re.search(r"\b(no-store|no-cache)\b", control, re.I):
					raise ValueError("Certificate response has no usable cache lifetime.")
				# Age accounts for upstream HTTP caches; subtract fetch duration too.
				ttl = min(int(match[1]), 86400) - int(response.headers.get("Age", "0"))
				ttl = int(ttl - (time.monotonic() - started))
				if ttl <= 0:
					raise ValueError("Certificate response is already expired.")
			finally:
				response.close()
			entry = {"certificates": certificates, "expires_at": time.time() + ttl}
			cache.set_value(CACHE_KEY, entry, expires_in_sec=ttl)
			_log_refresh(started, "ok")
			return entry
		except (requests.RequestException, ValueError, UnicodeError) as exc:
			_log_refresh(started, type(exc).__name__)
			# A refresh failure must not destroy still-fresh keys. Do not extend TTL.
			if entry := _cached():
				return entry
			raise CertificateUnavailableError("Firebase certificate refresh failed.") from exc
	finally:
		lock.release()


def _log_refresh(started: float, result: str) -> None:
	logger = frappe.logger("firebase_auth", allow_site=True)
	logger.setLevel("INFO")
	logger.info(
		json.dumps(
			{
				"stage": "certificate_refresh",
				"result": result,
				"seconds": round(time.monotonic() - started, 3),
			}
		)
	)


def refresh_firebase_certificates() -> None:
	"""Scheduled background refresh; no service account material is read or cached."""
	if not frappe.db.get_single_value("Flutter Utils Settings", "enable_firebase_auth"):
		return
	entry = _cached()
	if entry and entry["expires_at"] - time.time() > REFRESH_AHEAD_SECONDS:
		return
	try:
		_refresh()
	except CertificateUnavailableError:
		# The next scheduler run retries. Log only the safe category, never the
		# token-bearing request context that Frappe traceback locals may contain.
		frappe.log_error(
			title="Firebase Certificate Refresh Failed",
			message="No fresh Firebase signing certificates available; scheduled refresh will retry.",
		)
