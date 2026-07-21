import hashlib
import importlib
import importlib.util
import unittest

from src.common.published_policy import ResolvedIntegrationPolicy
from src.domain.integrations import IntegrationScope


DOMAIN = "example.com"


class FakeAuthStore:
    def __init__(self, session=None, user=None):
        self.session = session
        self.user = user
        self.session_hashes = []
        self.user_keys = []

    def get_session(self, session_hash):
        self.session_hashes.append(session_hash)
        return self.session

    def get_user(self, tenant_profile_key, user_key):
        self.user_keys.append((tenant_profile_key, user_key))
        return self.user


def auth_module(testcase):
    testcase.assertIsNotNone(
        importlib.util.find_spec("src.common.auth_admin"),
        "generic Auth Admin adapter is not implemented",
    )
    return importlib.import_module("src.common.auth_admin")


def policies(*, admin_capabilities=None, mode="auth-profile"):
    scope = IntegrationScope("test", "tenant-example", "draft-example", DOMAIN)
    if mode == "none":
        access = {"mode": "none"}
        registry = {}
    else:
        access = {
            "mode": "auth-profile",
            "authProfileId": "staff",
            "capabilities": tuple(
                admin_capabilities or ("integration:read", "integration:manage")
            ),
        }
        registry = {
            "version": 1,
            "profiles": (
                {
                    "authProfileId": "staff",
                    "status": "active",
                    "tenantId": "tenant-example",
                    "domain": DOMAIN,
                    "environment": "test",
                    "adminGroups": ("integration-admin",),
                    "allowedGroups": ("integration-admin", "viewer"),
                    "session": {
                        "csrfCookieName": "zlp_csrf",
                        "csrfHeaderName": "x-zlp-csrf",
                    },
                },
            ),
        }
    return ResolvedIntegrationPolicy(
        scope=scope,
        version_id="version-1",
        prefix="sites/example.com/versions/version-1/",
        bindings=(),
        admin_access=access,
        auth_registry=registry,
    )


def event(*, domain=DOMAIN, csrf=True, extra_headers=None):
    headers = {
        "x-zlp-domain": domain,
        "x-zlp-auth-profile-id": "staff",
        "cookie": "__Host-zlp_session=session-value; zlp_csrf=csrf-value",
    }
    if csrf:
        headers["x-zlp-csrf"] = "csrf-value"
    headers.update(extra_headers or {})
    return {"headers": headers}


def auth_store():
    return FakeAuthStore(
        session={
            "tenantProfileKey": f"{DOMAIN}#staff#test",
            "subject": "operator-1",
            "domain": DOMAIN,
            "authProfileId": "staff",
            "environment": "test",
            "tenantId": "tenant-example",
            "sessionVersion": 2,
            "expiresAt": 2_000,
            "csrfHash": hashlib.sha256(b"csrf-value").hexdigest(),
        },
        user={
            "sessionVersion": 2,
            "enabled": True,
            "approvalStatus": "approved",
            "roles": ["integration-admin"],
        },
    )


class AuthorizationTests(unittest.TestCase):
    def test_fresh_session_derives_exact_published_scope_and_checks_csrf(self):
        auth = auth_module(self)
        store = auth_store()

        context = auth.authorize_request(
            event=event(),
            policies=policies(),
            capability="integration:manage",
            mutation=True,
            store=store,
            now_epoch=1_000,
        )

        self.assertEqual(context.tenant_id, "tenant-example")
        self.assertEqual(context.draft_id, "draft-example")
        self.assertEqual(context.domain, DOMAIN)
        self.assertEqual(context.environment, "test")
        self.assertEqual(context.subject, "operator-1")
        self.assertEqual(
            context.session_hash,
            hashlib.sha256(b"session-value").hexdigest(),
        )
        self.assertEqual(store.user_keys, [(f"{DOMAIN}#staff#test", "USER#operator-1")])

    def test_provider_capabilities_never_authorize_a_human(self):
        auth = auth_module(self)
        with self.assertRaises(auth.AuthorizationError):
            auth.authorize_request(
                event=event(),
                policies=policies(admin_capabilities=("integration:read",)),
                capability="integration:manage",
                store=auth_store(),
                now_epoch=1_000,
            )
        with self.assertRaises(auth.AuthorizationError):
            auth.authorize_request(
                event=event(),
                policies=policies(),
                capability="connect-onboarding",
                store=auth_store(),
                now_epoch=1_000,
            )

    def test_none_mode_stale_state_missing_csrf_and_browser_scope_are_denied(self):
        auth = auth_module(self)
        stale = auth_store()
        stale.user["sessionVersion"] = 3
        cases = (
            (
                event(),
                policies(mode="none"),
                auth_store(),
                False,
                auth.AuthorizationError,
            ),
            (event(), policies(), stale, False, auth.AuthenticationError),
            (
                event(csrf=False),
                policies(),
                auth_store(),
                True,
                auth.AuthorizationError,
            ),
            (
                event(extra_headers={"x-zlp-draft-id": "draft-other"}),
                policies(),
                auth_store(),
                False,
                auth.AuthenticationError,
            ),
            (
                event(domain="other.example.com"),
                policies(),
                auth_store(),
                False,
                auth.AuthenticationError,
            ),
        )
        for request, resolved, store, mutation, error in cases:
            with self.subTest(error=error.__name__), self.assertRaises(error):
                auth.authorize_request(
                    event=request,
                    policies=resolved,
                    capability="integration:manage" if mutation else "integration:read",
                    mutation=mutation,
                    store=store,
                    now_epoch=1_000,
                )


if __name__ == "__main__":
    unittest.main()
