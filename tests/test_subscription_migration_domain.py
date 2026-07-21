import copy
import unittest

from src.subscription_migrations import (
    MigrationNeedsReview,
    build_immediate_plan,
    build_next_renewal_plan,
    canonical_migration_snapshot,
    validate_migration_offer_compatibility,
)
from tests.test_migration_contracts import offer


def item(item_id, price_id, quantity=1):
    return {
        "itemId": item_id,
        "priceId": price_id,
        "quantity": quantity,
        "taxRateIds": ["txr_synthetic"],
        "billingThresholds": None,
        "discountIds": [],
        "metadata": {},
        "priceConfiguration": {
            "currency": "MXN",
            "recurring": {
                "interval": "month",
                "intervalCount": 1,
                "usageType": "licensed",
            },
        },
    }


def phase(start, end, items):
    phase_items = [
        {key: value for key, value in line.items() if key != "itemId"}
        for line in items
    ]
    return {
        "startDate": start,
        "endDate": end,
        "items": phase_items,
        "discountIds": ["di_synthetic"],
        "automaticTax": {"enabled": False},
        "billingThresholds": None,
        "defaultTaxRateIds": ["txr_default"],
        "collectionMethod": "charge_automatically",
        "defaultPaymentMethodId": "pm_synthetic",
        "invoiceSettings": {"issuerType": "self", "daysUntilDue": None},
        "metadata": {
            "order_id": "order-1",
            "payment_attempt_id": "payment-attempt-1",
            "revision": "1",
        },
        "prorationBehavior": "none",
    }


def snapshot(*, phases=2):
    current_items = [item("si_primary", "price_old", 3), item("si_addon", "price_addon", 2)]
    schedule_phases = [
        phase(1_800_000_000 + index * 1_000, 1_800_001_000 + index * 1_000, copy.deepcopy(current_items))
        for index in range(phases)
    ]
    return {
        "subscriptionId": "sub_synthetic",
        "providerRevision": "a" * 64,
        "status": "active",
        "currency": "MXN",
        "currentPeriodStart": 1_800_000_000,
        "currentPeriodEnd": 1_800_001_000,
        "collectionMethod": "charge_automatically",
        "defaultPaymentMethodId": "pm_synthetic",
        "defaultPaymentMethodType": "card",
        "items": current_items,
        "discountIds": ["di_synthetic"],
        "automaticTax": {"enabled": False},
        "billingThresholds": None,
        "defaultTaxRateIds": ["txr_default"],
        "invoiceSettings": {"issuerType": "self", "daysUntilDue": None},
        "metadata": {
            "order_id": "order-1",
            "payment_attempt_id": "payment-attempt-1",
            "revision": "1",
        },
        "schedule": {
            "scheduleId": "sub_sched_synthetic",
            "status": "active",
            "endBehavior": "release",
            "currentPhaseIndex": 0,
            "defaultSettings": {
                "automaticTax": {"enabled": False},
                "billingCycleAnchor": "automatic",
                "billingThresholds": None,
                "collectionMethod": "charge_automatically",
                "defaultPaymentMethodId": "pm_synthetic",
                "invoiceSettings": {"issuerType": "self", "daysUntilDue": None},
            },
            "phases": schedule_phases,
        },
        "pendingUpdate": None,
        "latestInvoice": {
            "invoiceId": "in_synthetic",
            "status": "paid",
            "paymentStatus": "paid",
        },
        "pendingInvoiceItemCount": 0,
    }


class SubscriptionMigrationDomainTests(unittest.TestCase):
    def test_next_renewal_losslessly_preserves_thresholds_send_invoice_and_defaults(self):
        selected = snapshot()
        selected["collectionMethod"] = "send_invoice"
        selected["invoiceSettings"]["daysUntilDue"] = 15
        selected["billingThresholds"] = {
            "amountGte": 50_000,
            "resetBillingCycleAnchor": False,
        }
        selected["items"][0]["billingThresholds"] = {"usageGte": 25}
        selected["schedule"]["defaultSettings"] = {
            "automaticTax": {"enabled": False},
            "billingCycleAnchor": "automatic",
            "billingThresholds": {
                "amountGte": 50_000,
                "resetBillingCycleAnchor": False,
            },
            "collectionMethod": "send_invoice",
            "defaultPaymentMethodId": "pm_synthetic",
            "invoiceSettings": {"issuerType": "self", "daysUntilDue": 15},
        }
        for phase_value in selected["schedule"]["phases"]:
            phase_value["collectionMethod"] = "send_invoice"
            phase_value["invoiceSettings"]["daysUntilDue"] = 15
            phase_value["billingThresholds"] = {
                "amountGte": 50_000,
                "resetBillingCycleAnchor": False,
            }
            phase_value["items"][0]["billingThresholds"] = {"usageGte": 25}

        plan = build_next_renewal_plan(selected, "price_old", "price_new")

        self.assertEqual(plan["defaultSettings"], selected["schedule"]["defaultSettings"])
        self.assertEqual(plan["phases"][0]["billingThresholds"]["amountGte"], 50_000)
        self.assertEqual(plan["phases"][0]["items"][0]["billingThresholds"], {"usageGte": 25})
        self.assertEqual(plan["phases"][0]["invoiceSettings"]["daysUntilDue"], 15)
        with self.assertRaisesRegex(MigrationNeedsReview, "unsupported-collection-mode"):
            build_immediate_plan(
                selected,
                "price_old",
                "price_new",
                proration_timestamp=1_800_000_100,
                preview_amount_minor=1_000,
            )

    def test_existing_target_price_is_rejected_for_both_migration_policies(self):
        selected = snapshot()
        selected["items"].append(item("si_existing_target", "price_new"))
        for phase_value in selected["schedule"]["phases"]:
            phase_value["items"].append(
                {key: value for key, value in item("ignored", "price_new").items() if key != "itemId"}
            )

        with self.assertRaisesRegex(MigrationNeedsReview, "ambiguous-price"):
            build_next_renewal_plan(selected, "price_old", "price_new")

        selected["schedule"] = None
        with self.assertRaisesRegex(MigrationNeedsReview, "ambiguous-price"):
            build_immediate_plan(
                selected,
                "price_old",
                "price_new",
                proration_timestamp=1_800_000_100,
                preview_amount_minor=1_000,
            )

    def test_snapshot_contract_is_closed_and_rejects_pii_metadata(self):
        selected = snapshot()
        self.assertEqual(canonical_migration_snapshot(selected), selected)

        extra = copy.deepcopy(selected)
        extra["customerEmail"] = "forbidden@example.com"
        with self.assertRaises(MigrationNeedsReview):
            canonical_migration_snapshot(extra)

        unsafe_metadata = copy.deepcopy(selected)
        unsafe_metadata["metadata"] = {"email": "forbidden@example.com"}
        with self.assertRaises(MigrationNeedsReview):
            canonical_migration_snapshot(unsafe_metadata)

        pii_looking = copy.deepcopy(selected)
        pii_looking["metadata"] = {"zoolanding_customer_name": "Alice"}
        with self.assertRaises(MigrationNeedsReview):
            canonical_migration_snapshot(pii_looking)

    def test_snapshot_larger_than_safe_dynamo_budget_is_needs_review(self):
        selected = snapshot(phases=20)
        heavy_tax_ids = [
            "txr_" + str(index).zfill(2) + "x" * 180 for index in range(20)
        ]
        template = copy.deepcopy(selected["schedule"]["phases"][0]["items"][0])
        template["taxRateIds"] = heavy_tax_ids
        for phase_value in selected["schedule"]["phases"]:
            phase_value["items"] = [copy.deepcopy(template) for _ in range(20)]

        with self.assertRaisesRegex(MigrationNeedsReview, "snapshot-too-large"):
            canonical_migration_snapshot(selected)

    def test_provider_price_currency_cadence_and_offer_hash_are_bound(self):
        selected = snapshot()
        source = offer("offer-old", 90_000)
        target = offer("offer-new", 100_000)
        validate_migration_offer_compatibility(
            selected, "price_old", source, target
        )

        wrong_currency = copy.deepcopy(selected)
        wrong_currency["items"][0]["priceConfiguration"]["currency"] = "USD"
        with self.assertRaisesRegex(MigrationNeedsReview, "source-drift"):
            validate_migration_offer_compatibility(
                wrong_currency, "price_old", source, target
            )

        wrong_cadence = copy.deepcopy(selected)
        wrong_cadence["items"][0]["priceConfiguration"]["recurring"][
            "interval"
        ] = "year"
        with self.assertRaisesRegex(MigrationNeedsReview, "source-drift"):
            validate_migration_offer_compatibility(
                wrong_cadence, "price_old", source, target
            )

        wrong_hash = copy.deepcopy(source)
        wrong_hash["contentHash"] = "0" * 64
        with self.assertRaisesRegex(MigrationNeedsReview, "source-drift"):
            validate_migration_offer_compatibility(
                selected, "price_old", wrong_hash, target
            )

    def test_next_renewal_rebuilds_all_phases_and_replaces_only_the_exact_price(self):
        selected = snapshot()
        plan = build_next_renewal_plan(selected, "price_old", "price_new")

        self.assertEqual(
            set(plan),
            {
                "scheduleId",
                "defaultSettings",
                "endBehavior",
                "prorationBehavior",
                "phases",
            },
        )
        self.assertEqual(plan["endBehavior"], "release")
        self.assertEqual(plan["prorationBehavior"], "none")
        self.assertEqual(plan["phases"][0], selected["schedule"]["phases"][0])
        self.assertEqual(
            [line["priceId"] for line in plan["phases"][1]["items"]],
            ["price_new", "price_addon"],
        )
        self.assertEqual(plan["phases"][1]["items"][0]["quantity"], 3)
        self.assertEqual(
            plan["phases"][1]["items"][0]["taxRateIds"], ["txr_synthetic"]
        )
        self.assertTrue(
            all("itemId" not in line for item_phase in plan["phases"] for line in item_phase["items"])
        )
        self.assertEqual(
            plan["phases"][1]["metadata"],
            {
                "order_id": "order-1",
                "payment_attempt_id": "payment-attempt-1",
                "revision": "1",
            },
        )

    def test_next_renewal_refuses_more_than_ten_or_ambiguous_phases_and_never_cancels(self):
        with self.assertRaisesRegex(MigrationNeedsReview, "phase-limit"):
            build_next_renewal_plan(snapshot(phases=11), "price_old", "price_new")

        ambiguous = snapshot()
        duplicate = item("si_duplicate", "price_old")
        duplicate.pop("itemId")
        ambiguous["schedule"]["phases"][1]["items"].append(duplicate)
        with self.assertRaisesRegex(MigrationNeedsReview, "ambiguous-price"):
            build_next_renewal_plan(ambiguous, "price_old", "price_new")

        plan = build_next_renewal_plan(snapshot(), "price_old", "price_new")
        self.assertNotIn("cancel", repr(plan).lower())

    def test_no_schedule_uses_exact_period_dates_and_preserves_payment_method(self):
        selected = snapshot()
        selected["schedule"] = None
        plan = build_next_renewal_plan(selected, "price_old", "price_new")

        self.assertEqual(plan["phases"][0]["startDate"], selected["currentPeriodStart"])
        self.assertEqual(plan["phases"][0]["endDate"], selected["currentPeriodEnd"])
        self.assertEqual(
            plan["phases"][0]["defaultPaymentMethodId"], "pm_synthetic"
        )
        self.assertEqual(
            plan["phases"][1]["defaultPaymentMethodId"], "pm_synthetic"
        )

    def test_duration_only_schedule_phases_are_preserved_losslessly(self):
        selected = snapshot()
        for selected_phase in selected["schedule"]["phases"]:
            selected_phase.pop("endDate")
            selected_phase["duration"] = {"interval": "month", "intervalCount": 1}
        expected_future = copy.deepcopy(selected["schedule"]["phases"][1])
        expected_future["items"][0]["priceId"] = "price_new"
        expected_future["prorationBehavior"] = "none"

        plan = build_next_renewal_plan(selected, "price_old", "price_new")
        self.assertEqual(plan["phases"][0], selected["schedule"]["phases"][0])
        self.assertEqual(plan["phases"][1], expected_future)

    def test_immediate_plan_uses_exact_item_and_strict_eligibility(self):
        selected = snapshot()
        selected["schedule"] = None
        plan = build_immediate_plan(
            selected,
            "price_old",
            "price_new",
            proration_timestamp=1_800_000_100,
            preview_amount_minor=1_000,
        )
        self.assertEqual(
            plan,
            {
                "subscriptionId": "sub_synthetic",
                "itemId": "si_primary",
                "priceId": "price_new",
                "quantity": 3,
                "prorationTimestamp": 1_800_000_100,
                "prorationBehavior": "always_invoice",
                "paymentBehavior": "pending_if_incomplete",
            },
        )

        cases = (
            ("collectionMethod", "send_invoice", "unsupported-collection-mode"),
            ("defaultPaymentMethodType", "bank_transfer", "unsupported-payment-method"),
            ("pendingInvoiceItemCount", 1, "pending-invoice-items"),
            ("pendingUpdate", {"expiresAt": 1_800_000_500}, "pending-update"),
        )
        for field, value, reason in cases:
            with self.subTest(field=field):
                changed = copy.deepcopy(selected)
                changed[field] = value
                with self.assertRaisesRegex(MigrationNeedsReview, reason):
                    build_immediate_plan(
                        changed,
                        "price_old",
                        "price_new",
                        proration_timestamp=1_800_000_100,
                        preview_amount_minor=1_000,
                    )

        with self.assertRaisesRegex(MigrationNeedsReview, "nonpositive-proration"):
            build_immediate_plan(
                selected,
                "price_old",
                "price_new",
                proration_timestamp=1_800_000_100,
                preview_amount_minor=0,
            )


if __name__ == "__main__":
    unittest.main()
