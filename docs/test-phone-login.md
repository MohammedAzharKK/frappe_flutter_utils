# Test phone login (App Store / Play review)

Flutter Utils Settings supports one site-scoped demo phone number with a static
login OTP. This does not require enabling global **Test Mode** and does not alter
the public API contract or require client endpoint changes.

## Setup

1. Deploy the code and run `bench --site <site> migrate` to sync the settings fields.
2. Create a dedicated, enabled **Website User** with an international-format
   mobile number and representative demo data. Never use a personal account,
   System User, Administrator, System Manager, or Super Admin.
3. As a System Manager, open **Flutter Utils Settings**. Keep global **Test Mode**
   disabled in production and Mobile OTP enabled with its normal delivery setup.
4. Enable **Test Phone Login**, enter **Test Phone Number** (including `+` and
   country code), and set **Static OTP**. Its digit count must match **OTP Length**.
   Changing OTP Length affects all users, so verify client compatibility first.
5. Save and test the deployed app: request a mobile login OTP, then enter the static
   code. Confirm a subsequent authenticated read, not just credential issuance.
6. Give the dedicated account details and these instructions to the store reviewer.
   Keep access available throughout review and re-review; disable it deliberately
   when no longer needed. Treat the static code as a reusable account password.

## Security and scope

- The normal send endpoint must be called before every login. It stores an expiring
  challenge using the static code without calling an OTP delivery provider.
- Resend cooldown and existing endpoint rate limits still apply. Successful
  verification consumes the challenge and issues normal device credentials.
- Only mobile **login** uses the exception. Signup, email authentication, password
  reset, and contact verification do not use it. Other numbers are unchanged.
- Eligibility is rechecked during verification. Configuration changes (including
  disable/re-enable or code rotation), account reassignment, and privilege changes
  invalidate outstanding test challenges. Normal OTP challenges are unaffected.
- The code is stored in a Password field. Public OTP settings and send responses
  do not expose the demo number, code, or whether test-phone login was used, even
  when global Test Mode is on. Do not put credentials in source control or logs.
- Disabling this setting prevents further test logins; it does not revoke already
  issued device credentials. Revoke those separately if access must be removed.

## Verification

From the bench root, isolated tests (no database or delivery providers):

```sh
env/bin/python -m unittest flutter_utils.tests.test_test_phone_login -v
```

Also verify the settings form after migration and exercise real production/demo
and ordinary-number login flows before store submission. Unit tests mock storage,
delivery, and device credential issuance; they do not establish deployed behavior.
