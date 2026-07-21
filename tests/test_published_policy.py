import importlib
import io
import json
import unittest
from unittest import mock


DOMAIN = "example.com"
TENANT = "tenant-example"
DRAFT = "draft-example"


def policy_module():
    try:
        return importlib.import_module("src.common.published_policy")
    except ModuleNotFoundError as exc:
        raise AssertionError(
            "published Integration policy resolver is not implemented"
        ) from exc


def descriptor(*, mode="test", admin_access=None):
    return {
        "version": 1,
        "scope": {
            "environment": "test",
            "tenantId": TENANT,
            "draftId": DRAFT,
            "domain": DOMAIN,
        },
        "adminAccess": admin_access or {"mode": "none"},
        "bindings": [
            {
                "id": "stripe-primary",
                "provider": "stripe",
                "adapterVersion": "v1",
                "connectionId": "stripe-primary",
                "status": "active",
                "mode": mode,
                "capabilities": ["checkout", "one-time-payments"],
                "stripe": {
                    "accountStrategy": "oauth-standard-v1",
                    "accountModel": "merchant",
                    "chargeType": "direct",
                    "feePayer": "connected-account",
                    "taxMode": "unconfigured",
                    "platformFeeMode": "disabled",
                    "webhookIngress": "direct-integrations-api",
                },
            }
        ],
    }


class FakeTable:
    def __init__(self, metadata):
        self.metadata = metadata
        self.keys = []

    def get_item(self, **kwargs):
        self.keys.append(kwargs)
        return {"Item": self.metadata}


class FakeS3:
    def __init__(self, objects):
        self.objects = objects
        self.keys = []

    def get_object(self, **kwargs):
        self.keys.append(kwargs)
        value = self.objects[kwargs["Key"]]
        raw = value if isinstance(value, bytes) else json.dumps(value).encode("utf-8")
        return {"ContentLength": len(raw), "Body": io.BytesIO(raw)}


class PublishedPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = policy_module()
        self.version = "version-1"
        self.prefix = f"sites/{DOMAIN}/versions/{self.version}/"
        self.key = f"{self.prefix}{DOMAIN}/server/integration-bindings.json"
        self.auth_key = f"{self.prefix}{DOMAIN}/server/auth-profile-registry.json"
        self.commerce_key = f"{self.prefix}{DOMAIN}/server/commerce.json"
        self.table = FakeTable(
            {
                "pk": f"SITE#{DOMAIN}",
                "sk": "METADATA",
                "domain": DOMAIN,
                "serverScope": {"tenantId": TENANT, "draftId": DRAFT},
                "publishedEnvironments": {
                    "test": {"versionId": self.version, "prefix": self.prefix}
                },
            }
        )
        self.s3 = FakeS3({self.key: descriptor()})

    def resolver(self):
        return self.policy.PublishedPolicyResolver(
            self.table, self.s3, "zoolanding-config-payloads-test"
        )

    def test_resolves_only_the_exact_published_version_and_scope(self):
        resolved = self.resolver().resolve(
            environment="test",
            domain=DOMAIN,
            tenant_id=TENANT,
            draft_id=DRAFT,
        )
        self.assertEqual(resolved.version_id, self.version)
        self.assertEqual(resolved.prefix, self.prefix)
        self.assertEqual(resolved.bindings[0].connection_id, "stripe-primary")
        self.assertEqual(resolved.admin_access, {"mode": "none"})
        self.assertEqual(resolved.auth_registry, {})
        self.assertEqual(
            self.table.keys,
            [
                {
                    "Key": {"pk": f"SITE#{DOMAIN}", "sk": "METADATA"},
                    "ConsistentRead": True,
                }
            ],
        )
        self.assertEqual(
            self.s3.keys,
            [{"Bucket": "zoolanding-config-payloads-test", "Key": self.key}],
        )

    def test_rejects_cross_tenant_scope_and_prefix_confusion(self):
        with self.assertRaises(self.policy.PolicyResolutionError):
            self.resolver().resolve(
                environment="test",
                domain=DOMAIN,
                tenant_id="tenant-other",
                draft_id=DRAFT,
            )
        self.table.metadata["publishedEnvironments"]["test"][
            "prefix"
        ] = f"sites/{DOMAIN}/versions/{self.version}0/"
        with self.assertRaises(self.policy.PolicyResolutionError):
            self.resolver().resolve(environment="test", domain=DOMAIN)

    def test_rejects_duplicate_json_keys_invalid_utf8_size_and_depth(self):
        too_deep = 1
        for _ in range(self.policy.MAX_JSON_DEPTH + 1):
            too_deep = [too_deep]
        malformed_values = (
            b'{"version":1,"version":1}',
            b"\xff",
            b" " * (256 * 1024 + 1),
            json.dumps({"nested": too_deep}).encode(),
        )
        for raw in malformed_values:
            with self.subTest(length=len(raw)):
                self.s3.objects[self.key] = raw
                with self.assertRaises(self.policy.PolicyResolutionError):
                    self.resolver().resolve(environment="test", domain=DOMAIN)

        recursive = {}
        recursive["self"] = recursive
        with mock.patch(
            "src.common.published_policy.json.loads",
            side_effect=RecursionError("synthetic internals"),
        ):
            with self.assertRaises(self.policy.PolicyResolutionError) as failure:
                self.resolver().resolve(environment="test", domain=DOMAIN)
        self.assertNotIn("synthetic internals", str(failure.exception))

    def test_rejects_mode_mismatch_and_secret_material_without_echoing_it(self):
        self.s3.objects[self.key] = descriptor(mode="live")
        with self.assertRaises(self.policy.PolicyResolutionError) as mismatch:
            self.resolver().resolve(environment="test", domain=DOMAIN)
        self.assertNotIn("live", str(mismatch.exception))

        unsafe = descriptor()
        unsafe["bindings"][0]["privateToken"] = "synthetic-private-value"
        self.s3.objects[self.key] = unsafe
        with self.assertRaises(self.policy.PolicyResolutionError) as secret:
            self.resolver().resolve(environment="test", domain=DOMAIN)
        self.assertNotIn("synthetic-private-value", str(secret.exception))

    def test_auth_profile_access_loads_the_same_versioned_auth_registry(self):
        self.s3.objects[self.key] = descriptor(
            admin_access={
                "mode": "auth-profile",
                "authProfileId": "staff",
                "capabilities": ["integration:read", "integration:manage"],
            }
        )
        self.s3.objects[self.auth_key] = {
            "version": 1,
            "profiles": [
                {
                    "authProfileId": "staff",
                    "status": "active",
                    "tenantId": TENANT,
                    "domain": DOMAIN,
                    "environment": "test",
                }
            ],
        }

        resolved = self.resolver().resolve(environment="test", domain=DOMAIN)

        self.assertEqual(resolved.admin_access["authProfileId"], "staff")
        self.assertEqual(
            resolved.admin_access["capabilities"],
            ("integration:read", "integration:manage"),
        )
        self.assertEqual(resolved.auth_registry["profiles"][0]["tenantId"], TENANT)
        self.assertEqual(self.s3.keys[-1]["Key"], self.auth_key)

    def test_auth_profile_identifier_is_ascii_and_bounded(self):
        self.s3.objects[self.key] = descriptor(
            admin_access={
                "mode": "auth-profile",
                "authProfileId": "stáff",
                "capabilities": ["integration:read"],
            }
        )
        self.s3.objects[self.auth_key] = {
            "version": 1,
            "profiles": [{"authProfileId": "stáff"}],
        }
        with self.assertRaises(self.policy.PolicyResolutionError):
            self.resolver().resolve(environment="test", domain=DOMAIN)

    def test_checkout_routes_are_loaded_from_the_same_published_version(self):
        self.s3.objects[self.commerce_key] = {
            "version": 1,
            "scope": {
                "environment": "test",
                "tenantId": TENANT,
                "draftId": DRAFT,
                "domain": DOMAIN,
            },
            "commerce": {
                "status": "active",
                "checkout": {
                    "successPath": "/pago/resultado",
                    "cancelPath": "/planes",
                    "termsPath": "/terminos",
                    "privacyPath": "/privacidad",
                    "refundPolicyPath": "/terminos#reembolsos",
                    "supportPath": "/contacto",
                },
            },
        }
        scope = self.resolver().resolve(environment="test", domain=DOMAIN).scope
        routes = self.policy.PublishedCheckoutRouteResolver(self.resolver()).resolve(
            scope
        )

        self.assertEqual(
            routes,
            {
                "successUrl": "https://test.zoolandingpage.com.mx/pago/resultado?draftDomain=example.com",
                "cancelUrl": "https://test.zoolandingpage.com.mx/planes?draftDomain=example.com",
            },
        )
        self.assertEqual(self.s3.keys[-1]["Key"], self.commerce_key)

    def test_checkout_routes_reject_absolute_or_ambiguous_paths(self):
        value = {
            "version": 1,
            "scope": {
                "environment": "test",
                "tenantId": TENANT,
                "draftId": DRAFT,
                "domain": DOMAIN,
            },
            "commerce": {
                "status": "active",
                "checkout": {
                    "successPath": "//evil.example/path",
                    "cancelPath": "/plans?draftDomain=other.example",
                    "termsPath": "/terms",
                    "privacyPath": "/privacy",
                    "refundPolicyPath": "/refunds",
                    "supportPath": "/support",
                },
            },
        }
        self.s3.objects[self.commerce_key] = value
        scope = self.resolver().resolve(environment="test", domain=DOMAIN).scope
        with self.assertRaises(self.policy.PolicyResolutionError):
            self.policy.PublishedCheckoutRouteResolver(self.resolver()).resolve(scope)


if __name__ == "__main__":
    unittest.main()
