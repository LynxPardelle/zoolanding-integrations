import os
from unittest.mock import patch
import unittest

from src.common.http import HttpError
from src.handlers.internal_command import configured_callers, require_internal_caller


ROLE = "arn:aws:iam::123456789012:role/services/commerce-test"
ASSUMED_ROLE = (
    "arn:aws:sts::123456789012:assumed-role/services/commerce-test/"
    "commerce-request-123"
)
PATHLESS_ASSUMED_ROLE = (
    "arn:aws:sts::123456789012:assumed-role/commerce-test/"
    "commerce-request-123"
)


def event(user_arn):
    return {"requestContext": {"identity": {"userArn": user_arn}}}


class InternalCallerIdentityTests(unittest.TestCase):
    def test_assumed_role_session_normalizes_to_the_configured_iam_role(self):
        require_internal_caller(event(ASSUMED_ROLE), {ROLE})

    def test_pathless_sts_role_name_resolves_one_exact_configured_role_path(self):
        require_internal_caller(event(PATHLESS_ASSUMED_ROLE), {ROLE})
        ambiguous = {
            ROLE,
            "arn:aws:iam::123456789012:role/other/commerce-test",
        }
        with self.assertRaises(HttpError):
            require_internal_caller(event(PATHLESS_ASSUMED_ROLE), ambiguous)

    def test_direct_iam_role_identity_remains_supported(self):
        require_internal_caller(event(ROLE), {ROLE})

    def test_other_or_malformed_principals_fail_closed(self):
        denied = (
            "arn:aws:iam::123456789012:role/services/other",
            "arn:aws:iam::123456789012:user/operator",
            "arn:aws:sts::123456789012:federated-user/operator",
            "arn:aws:sts::123456789012:assumed-role/services/commerce-test",
            "arn:aws:sts::123456789012:assumed-role/services/commerce-test/session/extra",
            "*",
            "",
            None,
        )
        for principal in denied:
            with self.subTest(principal=principal), self.assertRaises(HttpError):
                require_internal_caller(event(principal), {ROLE})

    def test_configured_callers_accept_only_comma_separated_iam_role_arns(self):
        second = "arn:aws:iam::123456789012:role/services/notifications-test"
        with patch.dict(os.environ, {"INTERNAL_CALLER_ARNS": f"{ROLE},{second}"}, clear=True):
            self.assertEqual(configured_callers(), {ROLE, second})

        invalid_values = (
            ASSUMED_ROLE,
            f"{ROLE},*",
            f"{ROLE},arn:aws:iam::123456789012:user/operator",
            "not-an-arn",
        )
        for value in invalid_values:
            with self.subTest(value=value), patch.dict(
                os.environ, {"INTERNAL_CALLER_ARNS": value}, clear=True
            ):
                self.assertEqual(configured_callers(), set())


if __name__ == "__main__":
    unittest.main()
