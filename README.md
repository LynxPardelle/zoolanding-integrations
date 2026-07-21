# Zoolanding Integrations

Generic, server-only connection, provider-mapping, and confirmed-provider-event service for Zoolanding drafts. Phase 4 is implemented and verified locally; nothing in this repository is deployed or provider-activated.

## Implemented local scope

- Immutable published-policy resolution for exact environment, tenant, draft, domain, and published version.
- Provider-neutral `IntegrationConnection` and `IntegrationBinding` records with draft-partitioned Registry mappings and 90-day technical receipts.
- Generic Auth Admin authorization with separate human `integration:read` and `integration:manage` capabilities. Provider capabilities never authorize a person.
- Generic connection registration and resolution for Stripe and the code-owned SMTP adapter metadata. Secret values are never accepted, stored, returned, or logged.
- Stripe Connect onboarding through one explicit Accounts v1 strategy per binding: externally owned Standard-account OAuth or a platform-created controller account with code-owned controller properties.
- Stripe Product/Price, presentation, Coupon/PromotionCode lifecycle, hosted Checkout, Checkout status, subscription change/discount/pause-collection, and restricted Customer Portal commands.
- Signed Connect webhook ingress, immutable hashed account routing, global replay protection, canonical provider re-fetch, exact normalized Commerce events, and a separate outgoing Stream relay.
- Conditional command receipts, provider-resource mappings, subscription projections, and operation claims scoped to the exact draft, connection, resource, and revision.

The implemented Stripe capability vocabulary is closed to `connect-onboarding`, `checkout`, `one-time-payments`, `subscriptions`, `prices`, `coupons`, and `customer-portal`. The generic SMTP binding exposes only `send`.

## Routes

Browser-facing protected routes:

- `POST /features/integrations/read`
- `POST /features/integrations/action`
- `POST /features/integrations/stripe/onboarding`

Public provider ingress:

- `POST /webhooks/stripe/connect`

AWS_IAM internal routes:

- `POST /internal/v1/integrations/connection-register`
- `POST /internal/v1/integrations/connection-resolve`
- `POST /internal/v1/stripe/offer`
- `POST /internal/v1/stripe/product-presentation`
- `POST /internal/v1/stripe/discount`
- `POST /internal/v1/stripe/discount-lifecycle`
- `POST /internal/v1/stripe/checkout`
- `GET /internal/v1/stripe/checkout-status`
- `POST /internal/v1/stripe/subscription/change`
- `POST /internal/v1/stripe/subscription/discount`
- `POST /internal/v1/stripe/subscription/pause`
- `POST /internal/v1/stripe/customer-portal`
- `POST /internal/v1/stripe/migrations/preview`
- `POST /internal/v1/stripe/migrations/execute`
- `POST /internal/v1/stripe/migrations/control`
- `GET /internal/v1/stripe/migrations/status`

The four migration routes are intentional typed, fail-closed boundaries. They make no provider or job mutation until the Phase 5 migration engine is implemented.

## Credential, account, and tax boundaries

Stripe Connect uses one code-derived, environment-scoped structured platform credential reference. Test and production remain separate; drafts and browser requests never choose its location or supply its values. SMTP credentials remain independently referenced per draft connection. The Registry stores only approved reference metadata and non-secret state.

Every binding must select its Accounts v1 strategy explicitly. Accounts v2 activation remains blocked until the approved Mexico Sandbox proof covers the required capabilities and topology. Account ownership, provider account references, routing claims, controller properties, tokens, and credential values never enter draft configuration or browser responses.

Production subscription mutations require an exact server-owned tax approval before any Stripe access. The approval binds environment, tenant, draft, domain, connection, hashed provider account, mode, command revision, and approval revision/hash. Missing, stale, corrupt, cross-account, or wrong-mode approval returns `needs_review` without a provider call. Test code may inject its verifier; production composition may not.

Hosted Checkout and Customer Portal URLs are validated ephemeral `no-store` responses. They are not persisted in Registry mappings, receipts, events, logs, examples, or documentation.

## Runtime dependencies

- `boto3==1.39.13` matches the approved Zoolanding Python service baseline.
- `stripe==15.3.1` is the official Stripe Python SDK and the sole new runtime dependency. It is isolated behind the Stripe adapter; provider-neutral domain code does not import it.

## Local verification

The final Phase 4 commit passed 182 unit and contract tests, dependency audit, Python compilation, SAM lint/build, and import verification for all 22 packaged handlers.

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python -m pip_audit --requirement requirements.txt
python -m compileall -q src tests
sam validate --lint
sam build --no-cached
```

A local SAM build requires Python 3.13 on `PATH`; the declared production runtime is not relaxed to match another workstation interpreter.

## Closed rollout boundary

- Phase 5 owns bulk subscription migration and replacement of the four fail-closed migration seams.
- Phase 8 owns AWS deployment, exact cross-service IAM identities, queues/tables/topics, alarms, quotas, environment credentials, webhook configuration, and provider-backed end-to-end/failure testing.
- Phase 9 owns per-draft pilot configuration, production tax/live approval, and activation.

There is no AWS `dev` stack, deployment workflow/profile, live credential, connected account, webhook endpoint, provider call, browser QA, end-to-end Stripe proof, or pilot activation recorded by Phase 4. Deployment and live activation remain NO-GO until their later gates are explicitly approved and verified.
