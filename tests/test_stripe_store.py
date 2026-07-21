import unittest

from src.domain.integrations import IntegrationScope
from src.registry import _deserialize, _serialize


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
                self.items[identity] = __import__(
                    "src.registry", fromlist=["_serialize"]
                )._serialize(current)
            elif "Delete" in operation:
                key = _deserialize(operation["Delete"]["Key"])
                self.items.pop((key["pk"], key["sk"]), None)


class StripeStoreTests(unittest.TestCase):
    def setUp(self):
        from src.stripe_store import DynamoStripeCommandStore

        self.client = FakeDynamo()
        self.store = DynamoStripeCommandStore("registry-table", client=self.client)

    def claim(
        self,
        key="private-key",
        request_hash="a" * 64,
        command_id="command-1",
        *,
        resource_type="offer",
        resource_id="offer-v1",
        dimension="immutable",
        revision=1,
        content_hash="b" * 64,
        attempted_at=1_000,
    ):
        return self.store.claim(
            SCOPE,
            "stripe-primary",
            key,
            request_hash,
            command_id,
            2_000,
            attempted_at,
            {
                "resourceType": resource_type,
                "resourceId": resource_id,
                "dimension": dimension,
                "revision": revision,
                "contentHash": content_hash,
            },
        )

    def test_claim_hashes_the_idempotency_key_and_rejects_a_different_request(self):
        self.assertIsNone(self.claim())
        replay = self.claim()
        self.assertEqual(replay["status"], "pending")
        self.assertEqual(replay["attemptedAt"], 1_000)
        self.assertNotIn("private-key", repr(self.client.put_calls))
        self.assertEqual(len(self.client.transactions[0]["TransactItems"]), 2)
        self.assertIn("StripeOperationClaim", repr(self.client.transactions[0]))

        with self.assertRaisesRegex(Exception, "conflict"):
            self.claim(request_hash="b" * 64, command_id="command-2")

        with self.assertRaisesRegex(Exception, "conflict"):
            self.claim(key="other-key", command_id="a" * 65, resource_id="offer-v2")

    def test_operation_claim_conflicts_on_same_revision_different_content(self):
        self.claim()
        with self.assertRaisesRegex(Exception, "conflict"):
            self.claim(
                key="second-key",
                request_hash="c" * 64,
                command_id="command-2",
                content_hash="d" * 64,
            )

    def test_operation_claim_accepts_only_code_owned_resource_dimensions(self):
        accepted = (
            ("offer", "immutable"),
            ("offer", "presentation"),
            ("offer", "lifecycle"),
            ("discount", "immutable"),
            ("discount", "presentation"),
            ("discount", "lifecycle"),
            ("checkout", "immutable"),
            ("subscription", "change"),
            ("subscription", "discount"),
            ("subscription", "pause"),
            ("customer-portal", "immutable"),
        )
        for index, (resource_type, dimension) in enumerate(accepted, start=1):
            with self.subTest(resource_type=resource_type, dimension=dimension):
                self.assertIsNone(
                    self.claim(
                        key=f"key-{index}",
                        request_hash=f"{index:064x}",
                        command_id=f"command-{index}",
                        resource_type=resource_type,
                        resource_id=f"resource-{index}",
                        dimension=dimension,
                    )
                )

        for resource_type, dimension in (
            ("offer", "pause"),
            ("subscription", "immutable"),
            ("customer-portal", "portal"),
            ("unknown", "immutable"),
        ):
            with (
                self.subTest(resource_type=resource_type, dimension=dimension),
                self.assertRaisesRegex(Exception, "conflict"),
            ):
                self.claim(
                    key=f"invalid-{resource_type}-{dimension}",
                    request_hash="f" * 64,
                    command_id="invalid-command",
                    resource_type=resource_type,
                    resource_id="resource-invalid",
                    dimension=dimension,
                )

    def test_subscription_projection_read_is_exactly_scoped_and_typed(self):
        key = (SCOPE.partition_key, "STRIPE_SUBSCRIPTION_PROJECTION#subscription-1")
        record = {
            "pk": SCOPE.partition_key,
            "sk": key[1],
            "itemType": "StripeSubscriptionProjection",
            **SCOPE.fields(),
            "subscriptionId": "subscription-1",
            "offerVersionId": "offer-v1",
            "status": "active",
            "currentPeriodEnd": 1_900_000_000,
            "sourceRevision": 2,
            "lastEventId": "evt-2",
            "lastEventCreatedAt": 1_800_000_000,
            "stateHash": "a" * 64,
        }
        self.client.items[key] = _serialize(record)

        self.assertEqual(
            self.store.get_subscription_projection(
                SCOPE, "stripe-primary", "subscription-1"
            ),
            {
                "subscriptionId": "subscription-1",
                "offerVersionId": "offer-v1",
                "status": "active",
                "sourceRevision": 2,
            },
        )

        for changed in (
            {**record, "tenantId": "tenant-other"},
            {**record, "subscriptionId": "subscription-other"},
            {**record, "sourceRevision": True},
            {**record, "unexpected": True},
        ):
            with self.subTest(changed=changed):
                self.client.items[key] = _serialize(changed)
                with self.assertRaisesRegex(Exception, "unavailable"):
                    self.store.get_subscription_projection(
                        SCOPE, "stripe-primary", "subscription-1"
                    )

    def test_persisted_operation_claim_validation_is_closed_and_scope_bound(self):
        from src.stripe_store import _validated_operation_record

        expected = {
            "resourceType": "offer",
            "resourceId": "offer-v1",
            "dimension": "immutable",
            "revision": 1,
            "contentHash": "b" * 64,
        }
        record = {
            "pk": SCOPE.partition_key,
            "sk": "STRIPEOP#stripe-primary#offer#offer-v1#immutable",
            "itemType": "StripeOperationClaim",
            "connectionId": "stripe-primary",
            **expected,
            "requestHash": "a" * 64,
            "commandId": "command-1",
            "status": "accepted",
            "attemptedAt": 1_000,
        }
        self.assertEqual(
            _validated_operation_record(
                record, SCOPE.partition_key, "stripe-primary", expected
            ),
            record,
        )
        corruptions = (
            {**record, "pk": "ENV#production#TENANT#other#DRAFT#other"},
            {**record, "sk": "STRIPEOP#stripe-primary#offer#other#immutable"},
            {**record, "requestHash": "invalid"},
            {**record, "commandId": "a" * 65},
            {**record, "attemptedAt": -1},
            {**record, "unexpected": True},
        )
        for changed in corruptions:
            with (
                self.subTest(changed=changed),
                self.assertRaisesRegex(Exception, "unavailable"),
            ):
                _validated_operation_record(
                    changed, SCOPE.partition_key, "stripe-primary", expected
                )

    def test_complete_conditionally_commits_receipt_mapping_and_code_owner(self):
        self.claim()
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

        transaction = self.client.transactions[-1]
        self.assertIn("requestHash = :requestHash", repr(transaction))
        self.assertNotIn("private-key", repr(transaction))
        self.assertEqual(
            self.store.get_mapping(SCOPE, "stripe-primary", "discount", "discount-v1"),
            mapping,
        )
        self.assertEqual(
            self.store.code_owner(SCOPE, "stripe-primary", "c" * 64),
            "discount-v1",
        )

    def test_store_rejects_transient_redirect_urls_and_pii(self):
        self.claim()
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
        self.claim()
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
            for operation in self.client.transactions[-1]["TransactItems"]
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
        self.claim(
            key="key-one",
            resource_type="discount",
            resource_id="discount-v1",
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
        self.claim(
            key="key-two",
            request_hash="d" * 64,
            command_id="command-2",
            resource_type="discount",
            resource_id="discount-v1",
            dimension="lifecycle",
            revision=2,
            content_hash="e" * 64,
        )
        self.store.complete(
            SCOPE,
            "stripe-primary",
            "key-two",
            "d" * 64,
            {"status": "accepted"},
            [
                {
                    **active,
                    "status": "retired",
                    "lifecycleRevision": 2,
                    "lifecycleHash": "e" * 64,
                }
            ],
        )
        self.assertIsNone(self.store.code_owner(SCOPE, "stripe-primary", code_hash))
        lifecycle_update = next(
            operation["Update"]
            for operation in self.client.transactions[-1]["TransactItems"]
            if "Update" in operation
            and "lifecycleRevision"
            in operation["Update"].get("ExpressionAttributeNames", {}).values()
        )
        self.assertEqual(
            lifecycle_update["ConditionExpression"],
            "attribute_not_exists(lifecycleRevision) AND "
            "attribute_not_exists(lifecycleHash)",
        )
        self.assertTrue(
            any(
                "Delete" in operation
                for operation in self.client.transactions[-1]["TransactItems"]
            )
        )

    def test_discount_presentation_persists_copy_without_mutating_code_or_provider_index(
        self,
    ):
        old_code_hash = "c" * 64
        self.claim(
            key="key-one",
            resource_type="discount",
            resource_id="discount-v1",
        )
        active = {
            "resourceType": "discount",
            "resourceId": "discount-v1",
            "revision": 1,
            "contentHash": "b" * 64,
            "couponId": "couponSynthetic01",
            "promotionCodeId": "promo_synthetic01",
            "eligibleOfferVersionIds": ["offer-v1"],
            "duration": "once",
            "durationInMonths": None,
            "redeemByEpoch": 1_900_000_000,
            "redemptionLimit": 100,
            "value": {"type": "percentage", "basisPoints": 1500},
            "codeHash": old_code_hash,
            "status": "active",
        }
        self.store.complete(
            SCOPE,
            "stripe-primary",
            "key-one",
            "a" * 64,
            {"status": "accepted"},
            [active],
            code_claim=old_code_hash,
        )
        self.claim(
            key="key-two",
            request_hash="f" * 64,
            command_id="command-2",
            resource_type="discount",
            resource_id="discount-v1",
            dimension="presentation",
            revision=1,
            content_hash="9" * 64,
        )
        self.store.complete(
            SCOPE,
            "stripe-primary",
            "key-two",
            "f" * 64,
            {"status": "accepted"},
            [
                {
                    **active,
                    "presentationRevision": 1,
                    "presentationHash": "9" * 64,
                    "displayName": "Summer promotion",
                    "displayDescription": "Server-only copy",
                }
            ],
        )

        operations = self.client.transactions[-1]["TransactItems"]
        mapping_update = next(
            operation["Update"]
            for operation in operations
            if "Update" in operation
            and "presentationRevision" in repr(operation["Update"])
        )
        self.assertIn(
            "displayName", mapping_update["ExpressionAttributeNames"].values()
        )
        self.assertIn(
            "displayDescription", mapping_update["ExpressionAttributeNames"].values()
        )
        self.assertEqual(
            mapping_update["ConditionExpression"],
            "attribute_not_exists(presentationRevision) AND "
            "attribute_not_exists(presentationHash)",
        )
        deletes = [
            operation["Delete"] for operation in operations if "Delete" in operation
        ]
        self.assertEqual(deletes, [])
        self.assertNotIn("promo_synthetic01", repr(mapping_update))

    def test_immutable_revision_cannot_overwrite_newer_lifecycle_or_presentation(self):
        current = {
            "pk": SCOPE.partition_key,
            "sk": "STRIPEMAP#stripe-primary#discount#discount-v1",
            "itemType": "StripeResourceMapping",
            "connectionId": "stripe-primary",
            "resourceType": "discount",
            "resourceId": "discount-v1",
            "revision": 1,
            "contentHash": "b" * 64,
            "couponId": "couponSynthetic01",
            "promotionCodeId": "promo_synthetic01",
            "eligibleOfferVersionIds": ["offer-v1"],
            "duration": "once",
            "durationInMonths": None,
            "redeemByEpoch": None,
            "redemptionLimit": None,
            "value": {"type": "percentage", "basisPoints": 1500},
            "status": "retired",
            "lifecycleRevision": 2,
            "lifecycleHash": "c" * 64,
            "presentationRevision": 2,
            "presentationHash": "d" * 64,
            "displayName": "Current presentation",
            "displayDescription": "Current description",
        }
        self.client.items[(current["pk"], current["sk"])] = _serialize(current)
        stale_revision = {
            **{
                key: value
                for key, value in current.items()
                if key not in {"pk", "sk", "itemType", "connectionId"}
            },
            "revision": 2,
            "status": "active",
            "lifecycleRevision": 1,
            "lifecycleHash": "e" * 64,
            "presentationRevision": 1,
            "presentationHash": "f" * 64,
            "displayName": "Stale presentation",
            "displayDescription": "Stale description",
        }

        update = self.store._mapping_write(SCOPE, "stripe-primary", stale_revision)[
            "Update"
        ]

        changed_fields = set(update["ExpressionAttributeNames"].values())
        self.assertEqual(changed_fields, {"revision"})
        self.assertEqual(
            update["ConditionExpression"],
            "revision = :expected AND contentHash = :expectedHash",
        )
        self.assertTrue(
            {
                "status",
                "lifecycleRevision",
                "lifecycleHash",
                "presentationRevision",
                "presentationHash",
                "displayName",
                "displayDescription",
            }.isdisjoint(changed_fields)
        )

    def test_receipt_can_complete_from_an_existing_durable_mapping(self):
        self.claim()
        self.store.complete(
            SCOPE,
            "stripe-primary",
            "private-key",
            "a" * 64,
            {"status": "accepted"},
            [],
        )
        receipt = self.claim()
        self.assertEqual(receipt["status"], "accepted")

    def test_checkout_provider_links_are_conditionally_bound_to_existing_mapping(self):
        from src.stripe_store import DynamoStripeCommandStore

        class Client:
            def __init__(self):
                self.calls = []

            def transact_write_items(self, **kwargs):
                self.calls.append(kwargs)

        client = Client()
        store = DynamoStripeCommandStore("registry-table", client=client)
        store.bind_checkout_objects(
            SCOPE,
            "stripe-primary",
            {
                "resourceType": "checkout",
                "resourceId": "attempt-1",
                "revision": 1,
                "orderId": "order-1",
                "paymentAttemptId": "attempt-1",
                "reservationId": "reservation-1",
                "offerVersionIds": ["offer-v1"],
                "sessionId": "cs_test_synthetic01",
                "status": "created",
            },
            payment_intent_id="pi_synthetic01",
            subscription_id="sub_synthetic01",
        )

        operations = client.calls[0]["TransactItems"]
        self.assertEqual(len(operations), 3)
        self.assertIn("attribute_not_exists(#paymentIntentId)", repr(operations[0]))
        self.assertIn("StripeObjectIndex", repr(operations[1:]))
        self.assertIn("payment-intent", repr(operations[1:]))
        self.assertIn("subscription", repr(operations[1:]))
        self.assertNotIn("customer", repr(operations).lower())


if __name__ == "__main__":
    unittest.main()
