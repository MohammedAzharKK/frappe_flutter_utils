import json
import random
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import frappe
import phonenumbers
import requests
from frappe import _
from frappe.auth import LoginManager
from frappe.email.doctype.email_account.email_account import EmailAccount
from frappe.rate_limiter import rate_limit

from flutter_utils.device_credentials import (
	hash_device_id,
	issue_device_api_credentials,
	logout_current_device,
	normalize_device_name,
)

OTP_PURPOSES = {"login", "signup", "reset_password", "verify_mobile"}
OTP_CHANNELS = {"email", "mobile"}
AUTH_MODES = {"token", "session"}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=20, seconds=60, methods="POST")
def login(
	usr: str | None = None,
	pwd: str | None = None,
	device_id: str | None = None,
	device_name: str | None = None,
	auth_mode: str = "token",
	otp: str | None = None,
	tmp_id: str | None = None,
) -> dict[str, Any]:
	"""Complete native Frappe password/2FA login before returning session or device credentials."""
	from frappe.twofactor import ExpiredLoginException

	auth_mode = normalize_auth_mode(auth_mode)
	if auth_mode == "token":
		hash_device_id(device_id)
		normalize_device_name(device_name)
	if bool(otp) != bool(tmp_id):
		frappe.throw(_("Both OTP and temporary login ID are required."), frappe.AuthenticationError)
	if not tmp_id and not (usr and pwd):
		frappe.throw(_("Email and password are required."), frappe.AuthenticationError)

	# Native login reads these values from form_dict, including on the OTP continuation.
	frappe.form_dict.update(usr=usr, pwd=pwd, otp=otp, tmp_id=tmp_id)
	if not otp:
		frappe.form_dict.pop("otp", None)
	login_manager = frappe.local.login_manager
	try:
		if (
			not tmp_id
			and get_flutter_utils_settings().test_mode
			and frappe.get_system_settings("two_factor_method") in ("Email", "SMS")
		):
			login_result = _login_with_test_otp(login_manager)
		else:
			login_result = login_manager.login()
	except (frappe.AuthenticationError, ExpiredLoginException) as error:
		if tmp_id and (
			isinstance(error, ExpiredLoginException)
			or frappe.local.response.get("message") == "Incomplete login details"
		):
			error.error_code = "login_challenge_expired"
			error.http_status_code = 401
			error.args = (_("Your verification session expired. Please sign in again."),)
		raise
	if login_result is False:
		return {
			"authenticated": False,
			"auth_mode": auth_mode,
			**{
				key: frappe.local.response[key]
				for key in ("verification", "tmp_id", "message", "redirect_to", "otp")
				if key in frappe.local.response
			},
		}

	user = frappe.get_doc("User", frappe.session.user)
	if auth_mode == "session":
		return build_browser_session_response(user)
	assert device_id is not None
	return issue_device_api_credentials(user, device_id, device_name)


def _login_with_test_otp(login_manager: LoginManager) -> bool | None:
	"""Mirror native LoginManager.login's first leg, replacing only challenge delivery."""
	import pyotp
	from frappe.twofactor import cache_2fa_data, get_otpsecret_for_, should_run_2fa

	# Keep native password policy and login triggers; never patch process-global Frappe functions.
	login_manager.run_trigger("before_login")
	if frappe.get_system_settings("disable_user_pass_login"):
		frappe.throw(_("Login with username and password is not allowed."), frappe.AuthenticationError)
	frappe.clear_cache(user=frappe.form_dict.get("usr"))
	login_manager.authenticate()
	if login_manager.force_user_to_reset_password():
		doc = frappe.get_doc("User", login_manager.user)
		frappe.local.response["redirect_to"] = doc._reset_password(send_email=False, password_expired=True)
		frappe.local.response["message"] = "Password Reset"
		return False

	if should_run_2fa(login_manager.user):
		secret = get_otpsecret_for_(login_manager.user)
		token = int(pyotp.TOTP(secret).now())
		tmp_id = frappe.generate_hash(length=8)
		cache_2fa_data(login_manager.user, token, secret, tmp_id)
		frappe.local.response.update(
			verification={
				"method": frappe.get_system_settings("two_factor_method"),
				"setup": True,
				"token_delivery": False,
				"prompt": _("Test mode: delivery skipped. The verification code is filled automatically."),
			},
			tmp_id=tmp_id,
			otp=pyotp.HOTP(secret).at(token),
		)
		return False

	frappe.form_dict.pop("pwd", None)
	login_manager.post_login()
	return None


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=10, seconds=60, methods="POST")
def send_otp(
	purpose: str,
	channel: str | None = None,
	email: str | None = None,
	mobile_no: str | None = None,
	full_name: str | None = None,
) -> dict:
	"""
	Sends an OTP for login or signup using the configured delivery backend.
	Channel defaults to mobile when a mobile number is provided, otherwise email.
	"""
	settings = get_flutter_utils_settings()
	purpose = normalize_otp_purpose(purpose)
	if purpose == "login" and "password" in frappe.form_dict:
		frappe.throw(
			_(
				"Password login must use flutter_utils.api.auth.login with native Frappe two-factor authentication."
			),
			frappe.AuthenticationError,
		)
	if purpose == "verify_mobile" and frappe.session.user == "Guest":
		frappe.throw(
			_("Sign in before verifying a mobile number."), frappe.AuthenticationError
		)
	context = resolve_otp_context(channel=channel, email=email, mobile_no=mobile_no)
	if purpose == "verify_mobile" and context["channel"] != "mobile":
		frappe.throw(_("Mobile verification requires a mobile number."))

	enforce_otp_resend_cooldown(purpose, context["channel"], context["recipient"])

	otp = generate_otp()

	if purpose == "login":
		user = validate_login_target(context)
		payload = {"otp": otp}
		recipient_name = user.full_name
	elif purpose == "reset_password":
		user = validate_reset_password_target(context)
		payload = {"otp": otp}
		recipient_name = user.full_name
	elif purpose == "verify_mobile":
		payload = {"otp": otp, "mobile_no": context["recipient"]}
		recipient_name = frappe.utils.get_fullname(frappe.session.user) or frappe.session.user
	else:
		payload = validate_signup_target(context, full_name=full_name, email=email)
		payload["otp"] = otp
		recipient_name = payload["full_name"]

	cache_key = build_otp_cache_key(purpose, context["channel"], context["recipient"])
	otp_set(
		cache_key,
		json.dumps(payload),
		ttl=get_otp_ttl_seconds(),
	)

	if settings.test_mode:
		record_otp_resend_cooldown(purpose, context["channel"], context["recipient"])
		return build_otp_send_response(otp)

	try:
		deliver_otp(
			channel=context["channel"],
			recipient=context["recipient"],
			otp=otp,
			context=purpose,
			full_name=recipient_name,
		)
	except Exception:
		otp_delete(cache_key)
		raise

	record_otp_resend_cooldown(purpose, context["channel"], context["recipient"])
	return build_otp_send_response()


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=20, seconds=60, methods="POST")
def verify_otp(
	purpose: str,
	otp: str,
	channel: str | None = None,
	email: str | None = None,
	mobile_no: str | None = None,
	new_password: str | None = None,
	device_id: str | None = None,
	device_name: str | None = None,
	auth_mode: str = "token",
) -> dict[str, Any]:
	"""
	Verifies an OTP for login, signup, or reset_password and returns auth credentials on success.
	For reset_password, new_password is required and the user's password is updated.
	"""
	purpose = normalize_otp_purpose(purpose)
	auth_mode = normalize_auth_mode(auth_mode)
	if purpose == "verify_mobile" and frappe.session.user == "Guest":
		frappe.throw(
			_("Sign in before verifying a mobile number."), frappe.AuthenticationError
		)
	if auth_mode == "session" and purpose != "login":
		frappe.throw(_("Browser session authentication is only available for login."))
	if purpose in {"login", "signup"} and auth_mode == "token":
		hash_device_id(device_id)
		normalize_device_name(device_name)
	context = resolve_otp_context(channel=channel, email=email, mobile_no=mobile_no)
	cache_key = build_otp_cache_key(purpose, context["channel"], context["recipient"])
	raw = otp_get(cache_key)
	data = json.loads(raw) if raw else None

	if not data or data.get("otp") != otp.strip():
		frappe.throw(_("Invalid or expired OTP."))

	otp_delete(cache_key)

	if purpose == "verify_mobile":
		return {"verified": True, "mobile_no": context["recipient"]}

	if purpose == "login":
		user = (
			get_enabled_user_by_email(context["recipient"])
			if context["channel"] == "email"
			else get_enabled_user_by_mobile(context["recipient"])
		)
		if auth_mode == "session":
			return create_browser_session(user)
		assert device_id is not None
		return issue_device_api_credentials(user, device_id, device_name)

	if purpose == "reset_password":
		user = (
			get_enabled_user_by_email(context["recipient"])
			if context["channel"] == "email"
			else get_enabled_user_by_mobile(context["recipient"])
		)
		set_user_password(user, new_password)
		return {"message": _("Password has been reset successfully.")}

	assert_signup_identity_available(data.get("email"), data.get("mobile_no"))
	assert device_id is not None
	user = create_user(
		full_name=data["full_name"],
		email=data["email"],
		mobile_no=data.get("mobile_no"),
	)
	return issue_device_api_credentials(user, device_id, device_name)


@frappe.whitelist(allow_guest=True)
def send_login_otp(email: str) -> dict:
	return send_otp(purpose="login", channel="email", email=email)


@frappe.whitelist(allow_guest=True)
def verify_login_otp(
	email: str,
	otp: str,
	device_id: str,
	device_name: str | None = None,
) -> dict[str, Any]:
	return verify_otp(
		purpose="login",
		channel="email",
		email=email,
		otp=otp,
		device_id=device_id,
		device_name=device_name,
	)


@frappe.whitelist(allow_guest=True)
def send_mobile_login_otp(mobile_no: str) -> dict:
	return send_otp(purpose="login", channel="mobile", mobile_no=mobile_no)


@frappe.whitelist(allow_guest=True)
def verify_mobile_login_otp(
	mobile_no: str,
	otp: str,
	device_id: str,
	device_name: str | None = None,
) -> dict[str, Any]:
	return verify_otp(
		purpose="login",
		channel="mobile",
		mobile_no=mobile_no,
		otp=otp,
		device_id=device_id,
		device_name=device_name,
	)


@frappe.whitelist(allow_guest=True)
def send_signup_otp(full_name: str, email: str) -> dict:
	return send_otp(purpose="signup", channel="email", full_name=full_name, email=email)


@frappe.whitelist(allow_guest=True)
def verify_signup_otp(
	email: str,
	otp: str,
	device_id: str,
	device_name: str | None = None,
) -> dict[str, Any]:
	return verify_otp(
		purpose="signup",
		channel="email",
		email=email,
		otp=otp,
		device_id=device_id,
		device_name=device_name,
	)


@frappe.whitelist(allow_guest=True)
def send_mobile_signup_otp(full_name: str, email: str, mobile_no: str) -> dict:
	return send_otp(
		purpose="signup",
		channel="mobile",
		full_name=full_name,
		email=email,
		mobile_no=mobile_no,
	)


@frappe.whitelist(allow_guest=True)
def verify_mobile_signup_otp(
	mobile_no: str,
	otp: str,
	device_id: str,
	device_name: str | None = None,
) -> dict[str, Any]:
	return verify_otp(
		purpose="signup",
		channel="mobile",
		mobile_no=mobile_no,
		otp=otp,
		device_id=device_id,
		device_name=device_name,
	)


@frappe.whitelist(allow_guest=True)
def send_password_reset_otp(email: str) -> dict:
	"""Sends a password-reset OTP to the given email address."""
	return send_otp(purpose="reset_password", channel="email", email=email)


@frappe.whitelist(allow_guest=True)
def verify_password_reset_otp(email: str, otp: str, new_password: str) -> dict:
	"""Verifies a password-reset OTP and sets a new password for the user."""
	return verify_otp(
		purpose="reset_password",
		channel="email",
		email=email,
		otp=otp,
		new_password=new_password,
	)


@frappe.whitelist(methods=["POST"])
def send_verify_mobile_otp(mobile_no: str) -> dict:
	"""Send an SMS OTP to a signed-in user so they can verify a contact number."""
	return send_otp(purpose="verify_mobile", channel="mobile", mobile_no=mobile_no)


@frappe.whitelist(methods=["POST"])
def verify_verify_mobile_otp(mobile_no: str, otp: str) -> dict:
	"""Confirm a contact-number OTP for the signed-in user."""
	return verify_otp(
		purpose="verify_mobile",
		channel="mobile",
		mobile_no=mobile_no,
		otp=otp,
	)


def normalize_otp_purpose(purpose: str) -> str:
	normalized = purpose.strip().lower()
	if normalized not in OTP_PURPOSES:
		frappe.throw(
			_("Purpose must be one of: login, signup, reset_password, verify_mobile.")
		)
	return normalized


def normalize_auth_mode(auth_mode: str) -> str:
	normalized = (auth_mode or "token").strip().lower()
	if normalized not in AUTH_MODES:
		frappe.throw(_("Authentication mode must be one of: token, session."))
	return normalized


def resolve_otp_context(
	channel: str | None = None, email: str | None = None, mobile_no: str | None = None
) -> dict[str, str]:
	normalized_email = normalize_email(email) if email else None
	normalized_mobile = normalize_mobile_number(mobile_no) if mobile_no else None

	selected_channel = (channel or "").strip().lower() or ("mobile" if normalized_mobile else "email")
	if selected_channel not in OTP_CHANNELS:
		frappe.throw(_("Channel must be one of: email, mobile."))

	if selected_channel == "email":
		if not normalized_email:
			frappe.throw(_("Email is required for email OTP."))
		return {"channel": "email", "recipient": normalized_email}

	if not normalized_mobile:
		frappe.throw(_("Mobile number is required for mobile OTP."))
	return {"channel": "mobile", "recipient": normalized_mobile}


def validate_login_target(context: dict[str, str]):
	if context["channel"] == "email":
		return get_enabled_user_by_email(context["recipient"])

	return get_enabled_user_by_mobile(context["recipient"])


def validate_reset_password_target(context: dict[str, str]):
	if context["channel"] != "email":
		frappe.throw(_("Password reset is only available via email."))

	user = get_enabled_user_by_email(context["recipient"])
	if user.user_type == "System User" and user.name in ("Administrator", "Guest"):
		frappe.throw(_("Password reset is not available for this account."))
	return user


def enforce_otp_resend_cooldown(purpose: str, channel: str, recipient: str) -> None:
	if otp_get(build_otp_cooldown_key(purpose, channel, recipient)):
		frappe.throw(_("Please wait before requesting another OTP."))


def record_otp_resend_cooldown(purpose: str, channel: str, recipient: str) -> None:
	settings = get_flutter_utils_settings()
	cooldown_seconds = int(settings.otp_resend_cooldown_seconds or 30)
	if cooldown_seconds <= 0:
		return
	otp_set(build_otp_cooldown_key(purpose, channel, recipient), "1", ttl=cooldown_seconds)


def build_otp_cooldown_key(purpose: str, channel: str, recipient: str) -> str:
	return f"otp_cooldown:{purpose}:{channel}:{recipient}"


def set_user_password(user, new_password: str | None) -> None:
	if not new_password or not new_password.strip():
		frappe.throw(_("A new password is required."))
	user.new_password = new_password
	user.save(ignore_permissions=True)
	frappe.db.commit()


def validate_signup_target(context: dict[str, str], full_name: str | None, email: str | None) -> dict:
	if not full_name or not full_name.strip():
		frappe.throw(_("Full name is required for signup."))

	normalized_email = normalize_email(email)
	if not normalized_email:
		frappe.throw(_("Email is required for signup."))

	assert_signup_identity_available(
		email=normalized_email,
		mobile_no=context["recipient"] if context["channel"] == "mobile" else None,
	)

	return {
		"full_name": full_name.strip(),
		"email": normalized_email,
		"mobile_no": context["recipient"] if context["channel"] == "mobile" else None,
	}


def assert_signup_identity_available(email: str | None, mobile_no: str | None = None) -> None:
	# Existence, not just "enabled": a disabled row still owns the identity and
	# inserting a second User with the same email/phone can only fail. A
	# self-deleted account must rejoin through sign-in instead.
	if email and frappe.db.exists("User", email):
		frappe.throw(
			_(
				"An account with this email already exists. Sign in instead, "
				"or contact support if you cannot access it."
			)
		)

	if mobile_no and frappe.get_all(
		"User",
		filters={"mobile_no": mobile_no},
		pluck="name",
		limit=1,
	):
		frappe.throw(
			_(
				"An account with this mobile number already exists. Sign in instead, "
				"or contact support if you cannot access it."
			)
		)


def build_otp_cache_key(purpose: str, channel: str, recipient: str) -> str:
	return f"{purpose}_otp:{channel}:{recipient}"


def generate_otp() -> str:
	settings = frappe.get_cached_doc("Flutter Utils Settings")
	length = int(settings.otp_length or 4)
	if length <= 0:
		length = 4
	min_val = 10 ** (length - 1)
	max_val = (10**length) - 1
	return str(random.randint(min_val, max_val))


def _otp_cache_key(name: str) -> str:
	return f"{frappe.local.site}|{name}"


def otp_set(name: str, value: str, ttl: int) -> None:
	frappe.cache().set(_otp_cache_key(name), value, ex=ttl)


def otp_get(name: str) -> str | None:
	raw = frappe.cache().get(_otp_cache_key(name))
	return raw.decode() if raw else None


def otp_delete(name: str) -> None:
	frappe.cache().delete(_otp_cache_key(name))


def normalize_email(email: str | None) -> str | None:
	if not email:
		return None
	normalized = email.strip().lower()
	return normalized or None


def normalize_mobile_number(mobile_no: str) -> str:
	raw_mobile = mobile_no.strip()
	if not raw_mobile:
		frappe.throw(_("A valid mobile number is required."))

	settings = get_flutter_utils_settings()
	default_region = get_default_phone_region(settings)

	try:
		parsed = phonenumbers.parse(raw_mobile, default_region if not raw_mobile.startswith("+") else None)
	except phonenumbers.NumberParseException:
		frappe.throw(_("A valid mobile number is required."))

	if not phonenumbers.is_possible_number(parsed) or not phonenumbers.is_valid_number(parsed):
		frappe.throw(_("A valid mobile number is required."))

	return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def get_default_phone_region(settings) -> str:
	if settings.otp_default_region:
		country_code = frappe.db.get_value("Country", settings.otp_default_region, "code")
		if country_code:
			return country_code.upper()

	system_country = frappe.db.get_single_value("System Settings", "country")
	if system_country:
		country_code = frappe.db.get_value("Country", system_country, "code")
		if country_code:
			return country_code.upper()

	return "IN"


def get_enabled_user_by_mobile(mobile_no: str):
	enabled_users = frappe.get_all(
		"User",
		filters={"mobile_no": mobile_no, "enabled": 1},
		fields=["name"],
		limit=2,
	)

	if len(enabled_users) > 1:
		frappe.throw(_("Multiple accounts use this mobile number. Please contact support."))
	if enabled_users:
		return frappe.get_doc("User", enabled_users[0].name)

	disabled_users = frappe.get_all(
		"User",
		filters={"mobile_no": mobile_no, "enabled": 0},
		fields=["name"],
		limit=1,
	)
	if disabled_users:
		frappe.throw(_("Your account has been disabled. Please contact the administrator."))
	frappe.throw(_("No active account found for this mobile number."))


def get_enabled_user_by_email(email: str):
	if not frappe.db.exists("User", email):
		frappe.throw(_("No active account found for this email."))
	enabled = frappe.db.get_value("User", email, "enabled")
	if not enabled:
		frappe.throw(_("Your account has been disabled. Please contact the administrator."))
	return frappe.get_doc("User", email)


def create_user(full_name: str, email: str, mobile_no: str | None = None) -> Any:
	if frappe.db.exists("User", email):
		# Never attempt a duplicate insert (User.name is the email). Self-deleted
		# identities rejoin through sign-in; admins handle the rest.
		frappe.throw(
			_(
				"An account with this email already exists. Sign in instead, "
				"or contact support if you cannot access it."
			)
		)
	user = frappe.new_doc("User")
	user.first_name = full_name
	user.email = email
	user.mobile_no = mobile_no
	user.enabled = 1
	user.new_password = frappe.generate_hash(length=20)
	user.send_welcome_email = 0
	user.insert(ignore_permissions=True)
	return user


@frappe.whitelist(allow_guest=True)
def get_otp_auth_settings() -> dict:
	"""
	Returns public OTP settings for frontend configuration (e.g., login screens).
	"""
	settings = get_flutter_utils_settings()
	default_region = get_default_phone_region(settings)
	return {
		"otp_length": int(settings.otp_length or 4),
		"otp_resend_cooldown_seconds": int(settings.otp_resend_cooldown_seconds or 30),
		"otp_default_region": default_region,
	}


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_session_context() -> dict[str, Any]:
	"""Return the current browser session without exposing API credentials."""
	if frappe.session.user == "Guest":
		return {"authenticated": False, "auth_mode": "session"}
	return build_browser_session_response(frappe.get_doc("User", frappe.session.user))


@frappe.whitelist(allow_guest=True, methods=["POST"])
def logout_session() -> dict[str, str]:
	"""End the current browser session and clear its cookies."""
	frappe.local.login_manager.logout()
	frappe.db.commit()
	return {"message": _("Logged out successfully.")}


def create_browser_session(user: Any) -> dict[str, Any]:
	from frappe.auth import LoginManager

	login_manager = LoginManager()
	login_manager.login_as(user.name)
	return build_browser_session_response(user)


def build_browser_session_response(user: Any) -> dict[str, Any]:
	from frappe.sessions import get_csrf_token

	return {
		"authenticated": True,
		"auth_mode": "session",
		"user": user.name,
		"full_name": user.full_name,
		"email": user.email,
		"mobile_no": user.mobile_no,
		"user_type": user.user_type,
		"roles": sorted(frappe.get_roles(user.name)),
		"csrf_token": get_csrf_token(),
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=20, seconds=60, methods="POST")
def firebase_session_login(id_token: str) -> dict:
	"""Verify Firebase identity and create a browser-compatible Frappe session."""
	from frappe.auth import LoginManager

	from flutter_utils.firebase_auth import resolve_firebase_user, verify_firebase_id_token

	identity = verify_firebase_id_token(id_token)
	user = resolve_firebase_user(identity)
	login_manager = LoginManager()
	login_manager.login_as(user.name)
	return {
		"auth_mode": "session",
		"full_name": user.full_name,
		"email": identity.email or user.email,
		"mobile_no": identity.phone_number or user.mobile_no,
		"provider": identity.provider,
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=20, seconds=60, methods="POST")
def firebase_token_login(
	id_token: str,
	device_id: str | None = None,
	device_name: str | None = None,
) -> dict[str, Any]:
	"""Verify Firebase identity and issue Frappe API credentials for native clients."""
	from flutter_utils.firebase_auth import resolve_firebase_user, verify_firebase_id_token

	hash_device_id(device_id)
	assert device_id is not None
	attempt_id = uuid4().hex
	with _firebase_login_timing("login_total", attempt_id):
		with _firebase_login_timing("verify_identity", attempt_id):
			identity = verify_firebase_id_token(id_token)
		with _firebase_login_timing("resolve_user", attempt_id):
			user = resolve_firebase_user(identity)
		with _firebase_login_timing("issue_credentials", attempt_id):
			response = issue_device_api_credentials(user, device_id, device_name)
		response.update({"auth_mode": "token", "provider": identity.provider})
	return response


@contextmanager
def _firebase_login_timing(stage: str, attempt_id: str) -> Iterator[None]:
	started = time.monotonic()
	result = "failed"
	try:
		yield
		result = "ok"
	finally:
		frappe.logger("firebase_auth", allow_site=True).info(
			json.dumps(
				{
					"stage": stage,
					"attempt_id": attempt_id,
					"result": result,
					"seconds": round(time.monotonic() - started, 3),
				}
			)
		)


@frappe.whitelist(methods=["POST"])
def logout_device() -> dict[str, str]:
	"""Revoke the managed credential used for the current request."""
	return logout_current_device()


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=10, seconds=60, methods="POST")
def link_firebase_identities(primary_id_token: str, secondary_id_token: str) -> dict:
	"""Link two independently verified Firebase identities to one Frappe user."""
	from flutter_utils.firebase_auth import (
		link_firebase_identities as link_verified_identities,
	)
	from flutter_utils.firebase_auth import (
		validate_recent_firebase_authentication,
		verify_firebase_id_token,
	)

	primary_identity = verify_firebase_id_token(primary_id_token)
	secondary_identity = verify_firebase_id_token(secondary_id_token)
	validate_recent_firebase_authentication(primary_identity)
	validate_recent_firebase_authentication(secondary_identity)
	user = link_verified_identities(primary_identity, secondary_identity)
	return {
		"linked": True,
		"user": user.name,
		"firebase_uids": [primary_identity.uid, secondary_identity.uid],
	}


@frappe.whitelist(allow_guest=True)
def get_firebase_auth_settings() -> dict:
	"""Return enabled Firebase providers without exposing Firebase credentials."""
	settings = get_flutter_utils_settings()
	return {
		"enabled": bool(settings.enable_firebase_auth),
		"providers": {
			"phone": bool(settings.enable_firebase_auth and settings.enable_firebase_phone_auth),
			"google": bool(settings.enable_firebase_auth and settings.enable_firebase_google_auth),
		},
	}


def deliver_otp(channel: str, recipient: str, otp: str, context: str, full_name: str | None = None) -> None:
	if channel == "email":
		send_otp_email(recipient, otp, context)
		return

	if channel == "mobile":
		send_otp_sms(recipient, otp, context, full_name)
		return

	frappe.throw(_("Unsupported OTP delivery channel."))


def send_otp_email(email: str, otp: str, context: str) -> None:
	settings = get_flutter_utils_settings()
	if not settings.enable_email_otp:
		frappe.throw(_("Email OTP is disabled in Flutter Utils Settings."))

	subject, message = get_email_otp_message(otp=otp, context=context)
	ensure_email_otp_delivery_is_configured()

	frappe.sendmail(recipients=[email], subject=subject, message=message, now=True)


def ensure_email_otp_delivery_is_configured() -> None:
	email_account = EmailAccount.find_outgoing(_raise_error=True)
	if email_account.service == "Sendgrid HTTP":
		api_key = email_account.get_password("password") or frappe.conf.get("sendgrid_api_key")
		if not api_key:
			frappe.throw(_("SendGrid API key is not configured for the outgoing email account."))
		return

	if email_account.service != "Frappe Mail":
		email_account.get_smtp_server()


def send_otp_sms(mobile_no: str, otp: str, context: str, full_name: str | None = None) -> None:
	settings = get_flutter_utils_settings()
	if not settings.enable_mobile_otp:
		frappe.throw(_("Mobile OTP is disabled in Flutter Utils Settings."))

	sms_gateway = (settings.sms_gateway or "").strip()
	if sms_gateway == "Twilio":
		send_twilio_sms(
			account_sid=settings.twilio_account_sid,
			auth_token=settings.get_password("twilio_auth_token"),
			from_number=settings.twilio_from_number,
			mobile_no=mobile_no,
			otp=otp,
			context=context,
			full_name=full_name,
		)
		return
	if sms_gateway == "UltraMsg":
		send_ultramsg_sms(
			base_url=settings.ultramsg_base_url,
			instance_id=settings.ultramsg_instance_id,
			token=settings.get_password("ultramsg_token"),
			mobile_no=mobile_no,
			otp=otp,
			context=context,
			full_name=full_name,
		)
		return

	frappe.throw(_("Unsupported SMS gateway. Configure Flutter Utils Settings first."))


def send_twilio_sms(
	account_sid: str | None,
	auth_token: str | None,
	from_number: str | None,
	mobile_no: str,
	otp: str,
	context: str,
	full_name: str | None = None,
) -> None:
	if not account_sid or not auth_token or not from_number:
		frappe.throw(_("Twilio is not fully configured in Flutter Utils Settings."))

	message = get_sms_otp_message(otp=otp, context=context, full_name=full_name)

	response = requests.post(
		f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
		data={"To": mobile_no, "From": from_number, "Body": message},
		auth=(account_sid, auth_token),
		timeout=15,
	)
	if response.status_code >= 400:
		frappe.log_error(
			title="Twilio OTP SMS Failed",
			message=f"Status: {response.status_code}\nResponse: {response.text}",
		)
		frappe.throw(_("Failed to send OTP SMS. Please try again later."))


def send_ultramsg_sms(
	base_url: str | None,
	instance_id: str | None,
	token: str | None,
	mobile_no: str,
	otp: str,
	context: str,
	full_name: str | None = None,
) -> None:
	if not instance_id or not token:
		frappe.throw(_("UltraMsg is not fully configured in Flutter Utils Settings."))

	message = get_sms_otp_message(otp=otp, context=context, full_name=full_name)
	api_base_url = (base_url or "https://api.ultramsg.com").rstrip("/")

	response = requests.post(
		f"{api_base_url}/{instance_id}/messages/chat",
		data={"token": token, "to": mobile_no, "body": message},
		timeout=15,
	)
	if response.status_code >= 400:
		frappe.log_error(
			title="UltraMsg OTP SMS Failed",
			message=f"Status: {response.status_code}\nResponse: {response.text}",
		)
		frappe.throw(_("Failed to send OTP message through UltraMsg. Please try again later."))


def get_flutter_utils_settings():
	try:
		return frappe.get_single("Flutter Utils Settings")
	except frappe.DoesNotExistError:
		frappe.throw(_("Flutter Utils Settings is not available. Run bench migrate first."))


def send_configured_test_message(
	settings_name: str = "Flutter Utils Settings", channel: str | None = None, recipient: str | None = None
) -> dict:
	settings = frappe.get_doc("Flutter Utils Settings", settings_name)
	test_channel = (channel or "").strip().lower()
	message = "Flutter Utils integration test message."

	if test_channel == "email":
		if not recipient:
			frappe.throw(_("Test Email is required for email integration testing."))
		if not settings.enable_email_otp:
			frappe.throw(_("Enable Email OTP before sending a test email."))
		frappe.sendmail(
			recipients=[recipient.strip()],
			subject="Flutter Utils Test Message",
			message=message,
			now=True,
		)
		return {"message": _("Test email sent successfully.")}

	if test_channel == "mobile":
		if not recipient:
			frappe.throw(_("Test Mobile Number is required for mobile integration testing."))
		normalized_mobile = normalize_mobile_number(recipient)
		deliver_test_mobile_message(settings, normalized_mobile, message)
		return {"message": _("Test mobile message sent successfully.")}

	frappe.throw(_("Select a Test Channel before sending a test message."))


def get_otp_ttl_seconds() -> int:
	settings = get_flutter_utils_settings()
	return int(settings.otp_ttl_seconds or 600)


def get_otp_template_context(otp: str, context: str, full_name: str | None = None) -> dict[str, str | int]:
	ttl_seconds = get_otp_ttl_seconds()
	if context == "signup":
		action = _("complete your signup")
	elif context == "reset_password":
		action = _("reset your password")
	else:
		action = _("sign in")
	return {
		"app_name": (
			frappe.db.get_single_value("Website Settings", "app_name")
			or frappe.get_system_settings("app_name")
			or frappe.local.site
		),
		"otp": otp,
		"action": action,
		"expiry_minutes": max(1, ttl_seconds // 60),
		"expiry_seconds": ttl_seconds,
		"full_name": full_name or "",
	}


def render_setting_template(template: str, values: dict[str, str | int]) -> str:
	rendered = template
	for key, value in values.items():
		replacement = str(value)
		rendered = rendered.replace(f"{{{{ {key} }}}}", replacement)
		rendered = rendered.replace(f"{{{{{key}}}}}", replacement)
	return rendered


def get_email_otp_message(otp: str, context: str) -> tuple[str, str]:
	settings = get_flutter_utils_settings()
	template_values = get_otp_template_context(otp=otp, context=context)

	subject_template = settings.email_otp_subject_template or (
		_("Verify your email - {{ app_name }}")
		if context == "signup"
		else _("Your password reset OTP - {{ app_name }}")
		if context == "reset_password"
		else _("Your Login OTP - {{ app_name }}")
	)
	body_template = settings.email_otp_body_template or default_email_otp_body_template()

	return (
		render_setting_template(subject_template, template_values),
		render_setting_template(body_template, template_values),
	)


def get_sms_otp_message(otp: str, context: str, full_name: str | None = None) -> str:
	settings = get_flutter_utils_settings()
	template_values = get_otp_template_context(otp=otp, context=context, full_name=full_name)
	body_template = (
		settings.sms_otp_body_template
		or "{{ full_name }}{{ otp }} is your OTP to {{ action }} on {{ app_name }}. It expires in {{ expiry_minutes }} minutes."
	)
	return render_setting_template(body_template, template_values).strip()


def deliver_test_mobile_message(settings, mobile_no: str, message: str) -> None:
	sms_gateway = (settings.sms_gateway or "").strip()
	if sms_gateway == "Twilio":
		send_twilio_plain_message(
			account_sid=settings.twilio_account_sid,
			auth_token=settings.get_password("twilio_auth_token"),
			from_number=settings.twilio_from_number,
			mobile_no=mobile_no,
			message=message,
		)
		return
	if sms_gateway == "UltraMsg":
		send_ultramsg_plain_message(
			base_url=settings.ultramsg_base_url,
			instance_id=settings.ultramsg_instance_id,
			token=settings.get_password("ultramsg_token"),
			mobile_no=mobile_no,
			message=message,
		)
		return

	frappe.throw(_("Unsupported SMS gateway. Configure Flutter Utils Settings first."))


def send_twilio_plain_message(
	account_sid: str | None,
	auth_token: str | None,
	from_number: str | None,
	mobile_no: str,
	message: str,
) -> None:
	if not account_sid or not auth_token or not from_number:
		frappe.throw(_("Twilio is not fully configured in Flutter Utils Settings."))

	response = requests.post(
		f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
		data={"To": mobile_no, "From": from_number, "Body": message},
		auth=(account_sid, auth_token),
		timeout=15,
	)
	if response.status_code >= 400:
		frappe.log_error(
			title="Twilio Test Message Failed",
			message=f"Status: {response.status_code}\nResponse: {response.text}",
		)
		frappe.throw(_("Failed to send test message through Twilio."))


def send_ultramsg_plain_message(
	base_url: str | None,
	instance_id: str | None,
	token: str | None,
	mobile_no: str,
	message: str,
) -> None:
	if not instance_id or not token:
		frappe.throw(_("UltraMsg is not fully configured in Flutter Utils Settings."))

	api_base_url = (base_url or "https://api.ultramsg.com").rstrip("/")
	response = requests.post(
		f"{api_base_url}/{instance_id}/messages/chat",
		data={"token": token, "to": mobile_no, "body": message},
		timeout=15,
	)
	if response.status_code >= 400:
		frappe.log_error(
			title="UltraMsg Test Message Failed",
			message=f"Status: {response.status_code}\nResponse: {response.text}",
		)
		frappe.throw(_("Failed to send test message through UltraMsg."))


def build_otp_send_response(otp: str | None = None) -> dict:
	response = {"message": _("OTP sent successfully.")}
	if otp is not None:
		response["otp"] = otp
		response["test_mode"] = 1
	return response


def default_email_otp_body_template() -> str:
	return """
	<div style="font-family:sans-serif;max-width:480px;margin:auto;padding:32px;">
		<h2 style="color:#3525CD;margin-bottom:4px;">{{ app_name }}</h2>
		<p>Use the OTP below to {{ action }}. It expires in <strong>{{ expiry_minutes }} minutes</strong>.</p>
		<div style="font-size:40px;font-weight:bold;letter-spacing:10px;color:#3525CD;
		            background:#f0efff;padding:24px;border-radius:12px;
		            text-align:center;margin:24px 0;">
			{{ otp }}
		</div>
		<p style="color:#888;font-size:13px;">
			If you did not request this, you can safely ignore this email.
		</p>
	</div>
	"""
