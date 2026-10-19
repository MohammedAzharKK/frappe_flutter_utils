from __future__ import annotations

from typing import Any

import frappe
from frappe.tests.utils import FrappeTestCase

from flutter_utils.api.app_settings import get_app_config
from flutter_utils.app_settings import (
	DEFAULT_UNAVAILABLE_MESSAGE,
	PLATFORM_APP_FIELDS,
	build_app_config,
	compare_versions,
	is_valid_version,
	normalize_platform,
	normalize_version,
)

SETTINGS_DOCTYPE = "Flutter Utils Settings"

APP_SETTINGS_FIELDS = (
	"enable_app_settings",
	"app_identifier",
	"app_support_email",
	"app_support_url",
	*(f"{platform}_{suffix}" for platform in ("android", "ios") for suffix in PLATFORM_APP_FIELDS),
)


def snapshot_settings() -> dict[str, Any]:
	"""Read persisted App Settings values so a test can restore them."""
	settings = frappe.get_single(SETTINGS_DOCTYPE)
	return {fieldname: settings.get(fieldname) for fieldname in APP_SETTINGS_FIELDS}


def restore_settings(values: dict[str, Any]) -> None:
	for fieldname, value in values.items():
		frappe.db.set_single_value(SETTINGS_DOCTYPE, fieldname, value)
	frappe.db.commit()


def clear_settings() -> None:
	for fieldname in APP_SETTINGS_FIELDS:
		frappe.db.set_single_value(SETTINGS_DOCTYPE, fieldname, None)
	frappe.db.commit()


def write_settings(**values: Any) -> None:
	clear_settings()
	persisted: dict[str, Any] = {"enable_app_settings": 1, "app_identifier": "my_app"}
	persisted.update(values)
	for fieldname, value in persisted.items():
		frappe.db.set_single_value(SETTINGS_DOCTYPE, fieldname, value)
	frappe.db.commit()


def settings_stub(**values: Any) -> frappe._dict:
	"""Build an in-memory settings stub for pure config assertions."""
	return frappe._dict({"enable_app_settings": 1, "app_identifier": "my_app", **values})


class TestVersionComparison(FrappeTestCase):
	def test_compares_numeric_parts_larger_than_ten(self) -> None:
		self.assertEqual(compare_versions("1.10.0", "1.9.0"), 1)
		self.assertEqual(compare_versions("1.9.0", "1.10.0"), -1)

	def test_treats_missing_parts_as_zero(self) -> None:
		self.assertEqual(compare_versions("1.2", "1.2.0"), 0)
		self.assertEqual(compare_versions("1.2.0.0", "1.2"), 0)
		self.assertEqual(compare_versions("1.2.1", "1.2"), 1)

	def test_orders_pre_release_below_release(self) -> None:
		self.assertEqual(compare_versions("1.0.0-beta", "1.0.0"), -1)
		self.assertEqual(compare_versions("1.0.0", "1.0.0-beta"), 1)
		self.assertEqual(compare_versions("1.0.0-beta.1", "1.0.0-beta.2"), -1)
		self.assertEqual(compare_versions("1.0.0-beta", "1.0.0-beta"), 0)

	def test_numeric_pre_release_parts_compare_numerically(self) -> None:
		self.assertEqual(compare_versions("1.0.0-beta.2", "1.0.0-beta.10"), -1)

	def test_ignores_leading_v_and_build_metadata(self) -> None:
		self.assertEqual(compare_versions("v1.2.3", "1.2.3"), 0)
		self.assertEqual(compare_versions("1.2.3+45", "1.2.3"), 0)
		self.assertEqual(compare_versions("1.2.3+45", "1.2.3+99"), 0)

	def test_normalizes_versions(self) -> None:
		self.assertEqual(normalize_version(" 1.2.3+9 "), "1.2.3")
		self.assertEqual(normalize_version("v2.0"), "2.0")
		self.assertEqual(normalize_version("1.0.0-rc.1"), "1.0.0-rc.1")
		self.assertEqual(normalize_version(""), "")
		self.assertEqual(normalize_version("latest"), "")
		self.assertEqual(normalize_version(None), "")

	def test_reports_validity(self) -> None:
		self.assertTrue(is_valid_version("1.2.3"))
		self.assertTrue(is_valid_version("1.2.3-rc.1+7"))
		self.assertFalse(is_valid_version("1.2.3.4.5.beta"))
		self.assertFalse(is_valid_version("release-2"))
		self.assertFalse(is_valid_version(None))

	def test_compare_rejects_malformed_versions(self) -> None:
		with self.assertRaises(ValueError):
			compare_versions("latest", "1.0.0")


class TestPlatformNormalization(FrappeTestCase):
	def test_lowercases_and_trims(self) -> None:
		self.assertEqual(normalize_platform(" Android "), "android")
		self.assertEqual(normalize_platform("IOS"), "ios")

	def test_rejects_unknown_platform(self) -> None:
		for value in ("blackberry", "", None):
			with self.assertRaises(frappe.ValidationError):
				normalize_platform(value)


class TestBuildAppConfig(FrappeTestCase):
	def test_reports_clear_state_when_disabled(self) -> None:
		config = build_app_config(
			settings_stub(
				enable_app_settings=0,
				android_current_version="2.0.0",
				android_minimum_version="1.0.0",
			),
			"android",
			"0.5.0",
		)

		self.assertFalse(config["enabled"])
		self.assertFalse(config["managed"])
		self.assertFalse(config["update_required"])
		self.assertFalse(config["update_available"])
		self.assertFalse(config["unavailable"])
		self.assertIsNone(config["current_version"])

	def test_flags_outdated_client(self) -> None:
		config = build_app_config(
			settings_stub(android_current_version="2.0.0", android_minimum_version="1.5.0"),
			"android",
			"1.4.0",
		)

		self.assertTrue(config["managed"])
		self.assertTrue(config["update_available"])
		self.assertTrue(config["update_required"])
		self.assertFalse(config["force_update"])
		self.assertEqual(config["current_version"], "2.0.0")
		self.assertEqual(config["minimum_version"], "1.5.0")

	def test_force_update_flag_is_reported(self) -> None:
		config = build_app_config(
			settings_stub(
				android_current_version="2.0.0",
				android_minimum_version="1.5.0",
				android_force_update=1,
			),
			"android",
			"1.4.0",
		)

		self.assertTrue(config["force_update"])

	def test_up_to_date_client_is_not_gated(self) -> None:
		config = build_app_config(
			settings_stub(
				android_current_version="2.0.0",
				android_minimum_version="1.5.0",
				android_force_update=1,
			),
			"android",
			"2.0.0",
		)

		self.assertFalse(config["update_required"])
		self.assertFalse(config["update_available"])
		self.assertFalse(config["force_update"])

	def test_malformed_client_version_is_never_reported_as_outdated(self) -> None:
		for version in ("latest", "", None, "1.2.3.4.5.beta"):
			config = build_app_config(
				settings_stub(
					android_current_version="2.0.0",
					android_minimum_version="1.5.0",
					android_force_update=1,
				),
				"android",
				version,
			)
			self.assertFalse(config["update_required"], version)
			self.assertFalse(config["update_available"], version)
			self.assertFalse(config["force_update"], version)

	def test_missing_minimum_reports_only_availability(self) -> None:
		config = build_app_config(settings_stub(android_current_version="2.0.0"), "android", "1.0.0")

		self.assertTrue(config["update_available"])
		self.assertFalse(config["update_required"])
		self.assertIsNone(config["minimum_version"])

	def test_unavailable_with_configured_message(self) -> None:
		config = build_app_config(
			settings_stub(
				android_current_version="2.0.0",
				android_unavailable=1,
				android_unavailable_message="Back at 9 PM IST.",
			),
			"android",
			"2.0.0",
		)

		self.assertTrue(config["unavailable"])
		self.assertEqual(config["unavailable_message"], "Back at 9 PM IST.")

	def test_unavailable_falls_back_to_default_message(self) -> None:
		config = build_app_config(
			settings_stub(ios_current_version="2.0.0", ios_unavailable=1), "ios", "2.0.0"
		)

		self.assertTrue(config["unavailable"])
		self.assertEqual(config["unavailable_message"], DEFAULT_UNAVAILABLE_MESSAGE)

	def test_platforms_are_configured_independently(self) -> None:
		settings = settings_stub(
			android_current_version="3.0.0",
			ios_current_version="1.0.0",
			ios_minimum_version="1.0.0",
			ios_unavailable=1,
		)

		android = build_app_config(settings, "android", "1.0.0")
		ios = build_app_config(settings, "ios", "0.9.0")

		self.assertTrue(android["update_available"])
		self.assertFalse(android["update_required"])
		self.assertFalse(android["unavailable"])
		self.assertTrue(ios["update_required"])
		self.assertTrue(ios["unavailable"])
		self.assertEqual(android["store_url"] or "", "")
		self.assertEqual(ios["platform"], "ios")

	def test_unmanaged_platforms_are_never_gated(self) -> None:
		for platform in ("web", "macos", "windows", "linux"):
			config = build_app_config(
				settings_stub(
					android_current_version="2.0.0",
					android_minimum_version="1.0.0",
					android_unavailable=1,
				),
				platform,
				"0.1.0",
			)
			self.assertFalse(config["managed"], platform)
			self.assertFalse(config["update_required"], platform)
			self.assertFalse(config["unavailable"], platform)

	def test_mismatched_app_identifier_is_not_gated(self) -> None:
		config = build_app_config(
			settings_stub(
				android_current_version="2.0.0",
				android_minimum_version="1.5.0",
				android_force_update=1,
				android_unavailable=1,
			),
			"android",
			"1.0.0",
			app="other_app",
		)

		self.assertFalse(config["managed"])
		self.assertFalse(config["update_required"])
		self.assertFalse(config["unavailable"])

	def test_matching_app_identifier_is_gated(self) -> None:
		config = build_app_config(
			settings_stub(android_current_version="2.0.0", android_minimum_version="1.5.0"),
			"android",
			"1.0.0",
			app="my_app",
		)

		self.assertTrue(config["managed"])
		self.assertTrue(config["update_required"])

	def test_publishes_support_and_store_details(self) -> None:
		config = build_app_config(
			settings_stub(
				android_current_version="2.0.0",
				android_store_url="https://play.google.com/store/apps/details?id=com.example",
				android_release_notes_url="https://example.com/changelog",
				android_released_on=frappe.utils.getdate("2026-09-30"),
				app_support_email="support@example.com",
				app_support_url="https://example.com/support",
			),
			"android",
			"2.0.0",
		)

		self.assertEqual(config["app"], "my_app")
		self.assertEqual(config["app_version"], "2.0.0")
		self.assertEqual(config["store_url"], "https://play.google.com/store/apps/details?id=com.example")
		self.assertEqual(config["release_notes_url"], "https://example.com/changelog")
		self.assertEqual(config["released_on"], "2026-09-30")
		self.assertEqual(config["support_email"], "support@example.com")
		self.assertEqual(config["support_url"], "https://example.com/support")


class TestGetAppConfigEndpoint(FrappeTestCase):
	def setUp(self) -> None:
		frappe.set_user("Administrator")
		self.original_settings = snapshot_settings()

	def tearDown(self) -> None:
		restore_settings(self.original_settings)

	def test_returns_published_state(self) -> None:
		write_settings(android_current_version="2.0.0", android_minimum_version="1.5.0")

		config = get_app_config(platform="android", app_version="1.0.0")

		self.assertEqual(config["platform"], "android")
		self.assertTrue(config["update_required"])

	def test_reports_clear_state_when_disabled(self) -> None:
		write_settings(android_current_version="2.0.0", android_minimum_version="1.5.0")
		frappe.db.set_single_value(SETTINGS_DOCTYPE, "enable_app_settings", 0)
		frappe.db.commit()

		config = get_app_config(platform="android", app_version="1.0.0")

		self.assertFalse(config["enabled"])
		self.assertFalse(config["managed"])
		self.assertFalse(config["update_required"])

	def test_rejects_unsupported_platform(self) -> None:
		with self.assertRaises(frappe.ValidationError):
			get_app_config(platform="symbian", app_version="1.0.0")

	def test_is_callable_without_authentication(self) -> None:
		previous_user = frappe.session.user
		frappe.set_user("Guest")
		try:
			config = get_app_config(platform="android")
			self.assertEqual(config["platform"], "android")
		finally:
			frappe.set_user(previous_user)

	def test_unavailable_message_does_not_shadow_response_envelope(self) -> None:
		write_settings(android_current_version="2.0.0", android_unavailable=1)

		config = get_app_config(platform="android", app_version="2.0.0")

		self.assertIn("unavailable_message", config)
		self.assertIn("unavailable", config)

	def test_does_not_write_to_the_settings_document(self) -> None:
		write_settings(android_current_version="2.0.0")

		get_app_config(platform="android", app_version="1.0.0")

		self.assertEqual(frappe.db.get_single_value(SETTINGS_DOCTYPE, "android_current_version"), "2.0.0")


class TestAppSettingsValidation(FrappeTestCase):
	def setUp(self) -> None:
		frappe.set_user("Administrator")
		self.original_settings = snapshot_settings()

	def tearDown(self) -> None:
		restore_settings(self.original_settings)

	def _validate(self) -> None:
		"""Validate freshly loaded settings so persisted values drive the check."""
		frappe.clear_cache(doctype=SETTINGS_DOCTYPE)
		frappe.get_doc(SETTINGS_DOCTYPE).run_method("validate")

	def _test_requires_current_version(self, **values: Any) -> None:
		write_settings(**values)
		with self.assertRaises(frappe.ValidationError):
			self._validate()

	def test_requires_current_version(self) -> None:
		self._test_requires_current_version(
			android_minimum_version="1.0.0",
			android_current_version=None,
		)

	def test_requires_current_version_for_each_configured_platform(self) -> None:
		self._test_requires_current_version(
			android_current_version="2.0.0",
			ios_minimum_version="1.0.0",
			ios_current_version=None,
		)

	def test_rejects_minimum_newer_than_current(self) -> None:
		write_settings(android_current_version="1.5.0", android_minimum_version="2.0.0")

		with self.assertRaises(frappe.ValidationError):
			self._validate()

	def test_rejects_malformed_current_version(self) -> None:
		write_settings(android_current_version="release-two")

		with self.assertRaises(frappe.ValidationError):
			self._validate()

	def test_rejects_malformed_minimum_version(self) -> None:
		write_settings(android_current_version="2.0.0", android_minimum_version="latest")

		with self.assertRaises(frappe.ValidationError):
			self._validate()

	def test_allows_a_single_configured_platform(self) -> None:
		write_settings(android_current_version="2.0.0")

		self._validate()

	def test_allows_blank_minimum_version(self) -> None:
		write_settings(android_current_version="1.5.0", android_minimum_version=None)

		self._validate()

	def test_allows_equal_current_and_minimum(self) -> None:
		write_settings(android_current_version="1.5.0", android_minimum_version="1.5.0")

		self._validate()

	def test_allows_non_version_configuration_alone(self) -> None:
		write_settings(android_unavailable=1, android_store_url="https://example.com/app")

		self._validate()

	def test_normalizes_versions_on_save(self) -> None:
		write_settings(android_current_version="1.5.0", android_minimum_version="v1.2.3+7")
		doc = frappe.get_doc(SETTINGS_DOCTYPE)
		doc.run_method("validate")

		self.assertEqual(doc.android_current_version, "1.5.0")
		self.assertEqual(doc.android_minimum_version, "1.2.3")

	def test_skips_validation_when_disabled(self) -> None:
		write_settings(
			enable_app_settings=0,
			android_current_version="not-a-version",
			android_minimum_version="also-not-a-version",
		)

		self._validate()
