# Copyright (c) 2026, CoreAxis Solutions and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from flutter_utils.app_settings import (
	PLATFORM_APP_FIELDS,
	compare_versions,
	normalize_version,
)


class FlutterUtilsSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		android_current_version: DF.Data | None
		android_force_update: DF.Check
		android_minimum_version: DF.Data | None
		android_release_notes_url: DF.Data | None
		android_released_on: DF.Date | None
		android_store_url: DF.Data | None
		android_unavailable: DF.Check
		android_unavailable_message: DF.SmallText | None
		app_identifier: DF.Data | None
		app_support_email: DF.Data | None
		app_support_url: DF.Data | None
		email_otp_body_template: DF.TextEditor | None
		email_otp_subject_template: DF.Data | None
		enable_app_settings: DF.Check
		enable_email_otp: DF.Check
		enable_firebase_auth: DF.Check
		enable_firebase_google_auth: DF.Check
		enable_firebase_phone_auth: DF.Check
		enable_mobile_otp: DF.Check
		default_banner_slideshow: DF.Link | None
		firebase_auto_create_users: DF.Check
		firebase_check_revoked_tokens: DF.Check
		firebase_project_id: DF.Data | None
		firebase_service_account_json: DF.Password | None
		ios_current_version: DF.Data | None
		ios_force_update: DF.Check
		ios_minimum_version: DF.Data | None
		ios_release_notes_url: DF.Data | None
		ios_released_on: DF.Date | None
		ios_store_url: DF.Data | None
		ios_unavailable: DF.Check
		ios_unavailable_message: DF.SmallText | None
		maximum_logged_in_devices: DF.Int
		ultramsg_base_url: DF.Data | None
		ultramsg_instance_id: DF.Data | None
		ultramsg_token: DF.Password | None
		otp_length: DF.Literal["4", "6"]
		otp_resend_cooldown_seconds: DF.Int
		otp_ttl_seconds: DF.Int
		otp_default_region: DF.Link | None
		sms_otp_body_template: DF.SmallText | None
		sms_gateway: DF.Literal["", "Twilio", "UltraMsg"] | None
		test_mode: DF.Check
		twilio_account_sid: DF.Data | None
		twilio_auth_token: DF.Password | None
		twilio_from_number: DF.Data | None
	# end: auto-generated types

	def validate(self) -> None:
		if not self.maximum_logged_in_devices or self.maximum_logged_in_devices < 1:
			frappe.throw(_("Maximum Logged-in Devices must be at least 1."))
		if self.otp_default_region:
			self.otp_default_region = self.otp_default_region.strip().upper()
		if not self.otp_ttl_seconds or self.otp_ttl_seconds < 30:
			frappe.throw(_("OTP TTL must be at least 30 seconds."))

		self._validate_app_settings()

		if not self.enable_email_otp and not self.enable_mobile_otp and not self.enable_firebase_auth:
			frappe.throw(_("Enable at least one authentication provider."))

		self._validate_firebase_settings()

		if not self.enable_mobile_otp or self.test_mode:
			return

		if self.sms_gateway not in {"Twilio", "UltraMsg"}:
			frappe.throw(_("Select a supported SMS gateway before enabling Mobile OTP."))

		required_fields = {}
		if self.sms_gateway == "Twilio":
			required_fields = {
				"twilio_account_sid": _("Twilio Account SID"),
				"twilio_auth_token": _("Twilio Auth Token"),
				"twilio_from_number": _("Twilio From Number"),
			}
		elif self.sms_gateway == "UltraMsg":
			required_fields = {
				"ultramsg_instance_id": _("UltraMsg Instance ID"),
				"ultramsg_token": _("UltraMsg Token"),
			}

		for fieldname, label in required_fields.items():
			value = (
				self.get_password(fieldname)
				if fieldname in {"twilio_auth_token", "ultramsg_token"}
				else self.get(fieldname)
			)
			if not value:
				frappe.throw(_("{0} is required when Mobile OTP is enabled.").format(label))

	def on_update(self) -> None:
		previous = self.get_doc_before_save()
		if not previous:
			return
		previous_limit = int(previous.maximum_logged_in_devices or 1)
		new_limit = int(self.maximum_logged_in_devices)
		if new_limit >= previous_limit:
			return
		frappe.enqueue(
			"flutter_utils.device_credentials.prune_device_credentials",
			maximum_devices=new_limit,
			enqueue_after_commit=True,
		)

	def _validate_app_settings(self) -> None:
		"""Keep published release state comparable and non-blocking by construction."""
		if not self.enable_app_settings:
			return

		for platform, label in (("android", _("Android")), ("ios", _("iOS"))):
			if not self._is_app_platform_configured(platform):
				continue

			versions = {}
			for fieldname, field_label in (
				(f"{platform}_current_version", _("Current Version")),
				(f"{platform}_minimum_version", _("Minimum Version")),
			):
				raw_value = self.get(fieldname)
				version = normalize_version(raw_value)
				if raw_value and not version:
					frappe.throw(
						_("{0} {1} is invalid. Use a dotted numeric version such as 1.2.3.").format(
							label, field_label
						)
					)
				versions[fieldname] = version

			current_version = versions[f"{platform}_current_version"]
			minimum_version = versions[f"{platform}_minimum_version"]

			# A Current Version is only required once the admin entered a version at
			# all, so `Temporarily Unavailable` can be set without a release number.
			if self._has_app_platform_version(platform) and not current_version:
				frappe.throw(_("{0} Current Version is required.").format(label))

			if minimum_version and compare_versions(minimum_version, current_version) > 0:
				frappe.throw(_("{0} Minimum Version cannot be newer than {0} Current Version.").format(label))

			# Persist the normalized value so clients and validation agree on the format.
			for fieldname, version in versions.items():
				if version:
					self.set(fieldname, version)

	def _is_app_platform_configured(self, platform: str) -> bool:
		"""A platform counts as configured once any of its release fields is set.

		Skipping untouched platforms keeps a single-platform deployment valid
		without forcing an admin to invent an iOS version for an Android-only app.
		"""
		return any(self.get(f"{platform}_{suffix}") for suffix in PLATFORM_APP_FIELDS)

	def _has_app_platform_version(self, platform: str) -> bool:
		"""Whether the admin entered a version that has to be comparable."""
		return any(self.get(f"{platform}_{suffix}") for suffix in ("current_version", "minimum_version"))

	def _validate_firebase_settings(self) -> None:
		if not self.enable_firebase_auth and not self.get("enable_firebase_push"):
			return

		if (
			self.enable_firebase_auth
			and not self.enable_firebase_phone_auth
			and not self.enable_firebase_google_auth
		):
			frappe.throw(_("Enable Firebase Phone Auth or Firebase Google Auth."))

		if not self.firebase_project_id or not self.firebase_project_id.strip():
			frappe.throw(_("Firebase Project ID is required."))
		self.firebase_project_id = self.firebase_project_id.strip()

		from flutter_utils.firebase_auth import parse_service_account_json

		service_account = parse_service_account_json(self.get_password("firebase_service_account_json"))
		if service_account.get("project_id") != self.firebase_project_id:
			frappe.throw(_("Firebase Project ID must match the service account project_id."))

	@frappe.whitelist()
	def send_test_message(self, channel: str, recipient: str):
		from flutter_utils.api.auth import send_configured_test_message

		return send_configured_test_message(self.name, channel=channel, recipient=recipient)

	@frappe.whitelist()
	def test_firebase_connection(self) -> dict:
		from flutter_utils.firebase_auth import test_firebase_connection

		return test_firebase_connection()
