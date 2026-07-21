import unittest

from src.domain.integrations import IntegrationScope
from src.registry import _deserialize


SCOPE = IntegrationScope("test", "tenant-example", "draft-example", "example.com")


class FakeDynamo:
    def __init__(self):
        self.items = {}
        self.put_calls = []
        self.transactions = []

    def get_item(self, **kwargs):
        key = _deserialize(kwargs["Key"])
        item = self.items.get((key["pk"], key["sk"]))
        return {"Item": item} if item is not None else {}

    def put_item(self, **kwargs):
        self.put_calls.append(kwargs)
        item = kwargs["Item"]
        plain = _deserialize(item)
        identity = (plain["pk"], plain["sk"])
        if identity in self.items:
            raise RuntimeError("conditional")
        self.items[identity] = item

    def transact_write_items(self, **kwargs):
        self.transactions.append(kwargs)
        for operation in kwargs["TransactItems"]:
            if "Put" in operation:
                item = operation["Put"]["Item"]
                plain = _deserialize(item)
                self.items[(plain["pk"], plain["sk"])] = item
            elif "Update" in operation:
                update = operation["Update"]
                key = _deserialize(update["Key"])
                identity = (key["pk"], key["sk"])
                current = _deserialize(self.items[identity])
                values = {
                    key: next(iter(value.values()))
                    for key, value in update["ExpressionAttributeValues"].items()
                }
                if "#status = :accepted" in update["UpdateExpression"]:
                    current["status"] = values[":accepted"]
                self.items[identity] = __import__("src.registry", fromlist=["_serialize"])._serialize(current)
            elif "Delete" in operation:
                key = _deserialize(operation["Delete"]["Key"])
                self.items.pop((key["pk"], key["sk"]), None)


class StripeStoreTests(unittest.TestCase):
    def setUp(self):
        from src.stripe_store import DynamoStripeCommandStore

        self.client = FakeDynamo()
        self.store = DynamoStripeCommandStore("registry-table", client=self.client)

    def test_claim_hashes_the_idempotency_key_and_rejects_a_different_request(self):
        self.assertIsNone(
            self.store.claim(SCOPE, "stripe-primary", "private-key", "a" * 64, "command-1", 2_000)
        )
        replay = self.store.claim(
            SCOPE, "stripe-primary", "private-key", "a" * 64, "command-1", 2_000
        )
        self.assertEqual(replay["status"], "pending")
        self.assertNotIn("private-key", repr(self.client.put_calls))

        with self.assertRaisesRegex(Exception, "conflict"):
            self.store.claim(
                SCOPE, "stripe-primary", "private-key", "b" * 64, "command-2", 2_000
            )

    def test_complete_conditionally_commits_receipt_mapping_and_code_owner(self):
        self.store.claim(
            SCOPE, "stripe-primary", "private-key", "a" * 64, "command-1", 2_000
        )
        mapping = {
            "resourceType": "discount",
            "resourceId": "discount-v1",
            "revision": 1,
            "contentHash": "b" * 64,
            "couponId": "couponSynthetic01",
            "promotionCodeId": "promo_synthetic01",
            "eligibleOfferVersionIds": ["offer-v1"],
            "status": "active",
        }
        self.store.complete(
            SCOPE,
            "stripe-primary",
            "private-key",
            "a" * 64,
            {"status": "accepted"},
            [mapping],
            code_claim="c" * 64,
        )

        transaction = self.client.transactions[0]
        self.assertIn("requestHash = :requestHash", repr(transaction))
        self.assertNotIn("private-key", repr(transaction))
        self.assertEqual(
            self.store.get_mapping(
                SCOPE, "stripe-primary", "discount", "discount-v1"
            ),
            mapping,
        )
        self.assertEqual(
            self.store.code_owner(SCOPE, "stripe-primary", "c" * 64),
            "discount-v1",
        )

    def test_store_rejects_transient_redirect_urls_and_pii(self):
        self.store.claim(
            SCOPE, "stripe-primary", "private-key", "a" * 64, "command-1", 2_000
        )
        for forbidden in (
            {"redirectUrl": "https://checkout.stripe.com/private"},
            {"email": "buyer@example.com"},
        ):
            with self.subTest(forbidden=next(iter(forbidden))):
                with self.assertRaises(ValueError):
                    self.store.complete(
                        SCOPE,
                        "stripe-primary",
                        "private-key",
                        "a" * 64,
                        {"status": "accepted"},
                        [
                            {
                                "resourceType": "checkout",
                                "resourceId": "attempt-1",
                                "revision": 1,
                                **forbidden,
                            }
                        ],
                    )

    def test_inverse_provider_indexes_are_hashed_conditional_and_revalidated(self):
        self.store.claim(
            SCOPE, "stripe-primary", "private-key", "a" * 64, "command-1", 2_000
        )
        mapping = {
            "resourceType": "offer",
            "resourceId": "offer-v1",
            "revision": 1,
            "contentHash": "b" * 64,
            "productId": "prod_synthetic01",
            "priceId": "price_synthetic01",
            "status": "active",
        }
        self.store.complete(
            SCOPE,
            "stripe-primary",
            "private-key",
            "a" * 64,
            {"status": "accepted"},
            [mapping],
        )
        inverse_puts = [
            operation["Put"]
            for operation in self.client.transactions[0]["TransactItems"]
            if "Put" in operation
            and "StripeObjectIndex" in repr(operation["Put"]["Item"])
        ]
        self.assertEqual(len(inverse_puts), 2)
        self.assertNotIn("price_synthetic01", repr(inverse_puts))
        self.assertTrue(
            all("ConditionExpression" in operation for operation in inverse_puts)
        )
        owner = self.store.object_owner(
            SCOPE, "stripe-primary", "price", "price_synthetic01"
        )
        self.assertEqual(owner["resourceId"], "offer-v1")

    def test_inactive_discount_conditionally_releases_its_code_claim(self):
        code_hash = "c" * 64
        self.store.claim(
            SCOPE, "stripe-primary", "key-one", "a" * 64, "command-1", 2_000
        )
        active = {
            "resourceType": "discount",
            "resourceId": "discount-v1",
            "revision": 1,
            "contentHash": "b" * 64,
            "couponId": "couponSynthetic01",
            "promotionCodeId": "promo_synthetic01",
            "eligibleOfferVersionIds": ["offer-v1"],
            "codeHash": code_hash,
            "status": "active",
        }
        self.store.complete(
            SCOPE,
            "stripe-primary",
            "key-one",
            "a" * 64,
            {"status": "accepted"},
            [active],
            code_claim=code_hash,
        )
        self.store.claim(
            SCOPE, "stripe-primary", "key-two", "d" * 64, "command-2", 2_000
        )
        self.store.complete(
            SCOPE,
            "stripe-primary",
            "key-two",
            "d" * 64,
            {"status": "accepted"},
            [{**active, "status": "retired", "lifecycleRevision": 2}],
        )
        self.assertIsNone(
            self.store.code_owner(SCOPE, "stripe-primary", code_hash)
        )
        self.assertTrue(
            any(
                "Delete" in operation
                for operation in self.client.transactions[-1]["TransactItems"]
            )
        )

    def test_receipt_can_complete_from_an_existing_durable_mapping(self):
        self.store.claim(
            SCOPE, "stripe-primary", "private-key", "a" * 64, "command-1", 2_000
        )
        self.store.complete(
            SCOPE,
            "stripe-primary",
            "private-key",
            "a" * 64,
            {"status": "accepted"},
            [],
        )
        receipt = self.store.claim(
            SCOPE, "stripe-primary", "private-key", "a" * 64, "command-1", 2_000
        )
        self.assertEqual(receipt["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
