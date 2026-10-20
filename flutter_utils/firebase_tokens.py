"""Firebase ID-token verification using public google-auth and Firebase APIs.

The Admin SDK does not publicly expose its certificate-cache transport. Use
google-auth's public JWT verifier with our shared public certificates rather than
patching SDK private clients or globally replacing requests/socket functions.
Firebase's RS256/kid/issuer/subject requirements and revocation policy are retained.
"""

from typing import Any

from firebase_admin import auth
from google.auth import jwt

from flutter_utils.firebase_certificates import get_certificates


def verify_id_token(id_token: str, *, app: Any, check_revoked: bool = False) -> dict[str, Any]:
	project_id = app.project_id
	try:
		header = jwt.decode_header(id_token)
		# Unverified fields are used only to reject invalid tokens early. Never
		# resolve an account or grant access until jwt.decode verifies the signature.
		claims = jwt.decode(id_token, verify=False)
		if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str) or not header["kid"]:
			raise ValueError("Invalid Firebase token algorithm or key ID.")
		if not project_id or claims.get("aud") != project_id:
			raise ValueError("Invalid Firebase token audience.")
		if claims.get("iss") != f"https://securetoken.google.com/{project_id}":
			raise ValueError("Invalid Firebase token issuer.")
		subject = claims.get("sub")
		if not isinstance(subject, str) or not 1 <= len(subject) <= 128:
			raise ValueError("Invalid Firebase token subject.")
		certificates = get_certificates(header["kid"])
		claims = jwt.decode(id_token, certs=certificates, audience=project_id)
	except (ValueError, TypeError, KeyError) as exc:
		# google-auth exposes expiration as ValueError; Firebase Admin uses the
		# same distinction to retain its public ExpiredIdTokenError contract.
		if isinstance(exc, ValueError) and "Token expired" in str(exc):
			raise auth.ExpiredIdTokenError("Firebase ID token has expired.", cause=exc) from exc
		raise auth.InvalidIdTokenError("Invalid Firebase ID token.", cause=exc) from exc
	claims["uid"] = claims["sub"]
	if check_revoked:
		# Match Firebase Admin's policy: compare issued-at (not auth_time), and
		# reject disabled users. This optional remote check is deliberately not cached.
		user = auth.get_user(claims["uid"], app=app)
		if user.disabled:
			raise auth.UserDisabledError("The Firebase user is disabled.")
		if claims["iat"] * 1000 < user.tokens_valid_after_timestamp:
			raise auth.RevokedIdTokenError("Firebase ID token has been revoked.")
	return claims
