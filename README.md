# Zoolanding Integrations

Generic, server-only connection, provider-mapping, confirmed-provider-event, bulk subscription-migration, and SMTP connection-metadata service for Zoolanding drafts. Phases 5 and 6 are implemented and verified locally; nothing in this repository is deployed or provider-activated.

## Implemented local scope

- Immutable published-policy resolution for exact environment, tenant, draft, domain, and published version.
- Provider-neutral `IntegrationConnection` and `IntegrationBinding` records with draft-partitioned Registry mappings and 90-day technical receipts.
- Generic Auth Admin authorization with separate human `integration:read` and `integration:manage` capabilities. Provider capabilities never authorize a person.
- Generic connection registration and resolution for Stripe and the code-owned SMTP adapter metadata. SMTP registration remains pending until a separate AWS_IAM activation proves fresh credential tags and operator evidence; secret values are never accepted, stored, returned, or logged.
- Stripe Connect onboarding through one explicit Accounts v1 strategy per binding: externally owned Standard-account OAuth or a platform-created controller account with code-owned controller properties.
- Stripe Product/Price, presentation, Coupon/PromotionCode lifecycle, hosted Checkout, Checkout status, subscription change/discount/pause-collection, and restricted Customer Portal commands.
- Signed Connect webhook ingress, immutable hashed account routing, global replay protection, canonical provider re-fetch, exact normalized Commerce events, and a separate outgoing Stream relay.
- Conditional command receipts, provider-resource mappings, subscription projections, and operation claims scoped to the exact draft, connection, resource, and revision.
- Bulk subscription dry runs and execution for exact source-to-target offer revisions, using immutable safe snapshots, durable canaries, bounded account concurrency, per-item leases/retries, pause/resume/cancel controls, and paginated status.
- Exact Stripe migration adapters for next-renewal schedules and immediate prorated changes. Unsupported or ambiguous provider state fails closed to `needs_review` without exposing provider payloads or recovery secrets.
- Pending-update webhook reconciliation for migration state, alongside the existing Commerce event projection. The closed Stripe ingress allowlist contains eleven Commerce-facing events, two migration pending-update events, and one Connect deauthorization event.

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
- `POST /internal/v1/integrations/smtp-connection-activate`
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

The four migration routes are implemented typed boundaries. Preview is provider-read-only and creates an expiring immutable dry run. Execute requires the exact dry-run revision/hash plus server-owned tax authorization. Control is optimistic-revision-bound; status is read-only and paginated.

## Bulk migration boundaries

- Discovery reads at most 100 subscriptions per provider page and rejects empty, repeated, or more than 1,000 continuation pages. This bounds one job to 100,000 discovered candidates.
- Worker and cancellation batches are capped at 25. Draft configuration may select a canary from 1 through 25 and provider-account concurrency from 1 through 5.
- Sparse DynamoDB work indexes select only bounded actionable states. A durable, replay-safe preview commitment and job counters finish dry runs, execution, and cancellation without loading every item; the status route remains the only paginated item-detail read.
- Provider mutation retries stop after five attempts. Transient failures are delayed; permanent, ambiguous, ownership-mismatched, or exhausted items become `needs_review` without stopping unrelated items.
- Next-renewal migration preserves the supported subscription, schedule, phase, invoice, threshold, item, quantity, tax-rate, and discount fields represented by the closed snapshot contract. Snapshots are capped at 300 KiB and reject metadata, customer identity, payment credentials, and unsupported provider fields.
- Immediate migration revalidates the exact dry-run timestamp and proration amount before mutation. Customer-action recovery remains on Stripe-hosted HTTPS invoice pages; URLs and PaymentIntent client secrets are neither persisted nor returned by this service.
- Cancel before mutation skips pending work. Cancel after a next-renewal migration releases or restores only the exact schedule proved to belong to that item. Completed next-renewal jobs may be rolled back through the same bounded workflow; immediate prorated jobs have no global rollback.
- Active authorization overlays are lifecycle-scoped: target offers are removed after rollback/cancel and review states do not broaden checkout authorization.

## Credential, account, and tax boundaries

Stripe Connect uses one code-derived, environment-scoped structured platform credential reference. Test and production remain separate; drafts and browser requests never choose its location or supply its values. SMTP credentials remain independently referenced per draft connection. The Registry stores only approved reference metadata, hashes, and non-secret state.

SMTP registration fixes `smtp2go-smtp-v1`, `mail.smtp2go.com:465`, and implicit TLS in code. It does not claim account ownership or readiness, and a disabled published binding cannot register. Activation is a separate AWS_IAM-only command with its own operator-role allowlist; general internal callers, including Notifications, cannot activate. It freshly rechecks the stored binding and rejects a disabled or mismatched binding before `DescribeSecret` or any claim write. It then calls `DescribeSecret`, never `GetSecretValue`, and requires the exact secret scope tags plus opaque account- and credential-isolation tags. The operator supplies conservative `fromLocalPart` and `replyToLocalPart` values and an opaque ownership-evidence ID; raw isolation and evidence IDs are hashed before Registry writes.

Test connections send through the server-owned shared sender domain while retaining each draft's independent canonical scope domain. Their account-isolation hash must equal the deployment parameter `SmtpTestSharedAccountClaimHash`, while credential claims remain globally unique. Production rejects that test account claim and atomically reserves a unique credential hash, account hash, and canonical sending domain. Exact activation replay is a no-op; changed evidence or a reused claim conflicts.

The private resolve result is available only for an active `email.smtp` binding with the exact `send` capability and scope. It returns the fixed adapter and TLS endpoint, local-part sender policy, deterministic credential reference, and opaque rate/circuit namespace. It never returns isolation/evidence hashes, full email addresses, credentials, or secret values. Browser connection projections remain limited to connection ID, provider, status, mode, capabilities, and revision.

The generic browser `disable` and `requestReconnect` operations reject SMTP connections before any Registry mutation. Emergency SMTP shutdown uses a disabled published policy plus `zoolanding:enabled=false` on the credential lifecycle tag. Credential rotation, reconnection, ownership transfer, and claim release require a later dedicated transactional workflow; Phase 6 does not pretend those operations are supported. Stripe browser disable and reconnect behavior is unchanged.

Before a later deployment, provision the SMTP secret at `/zoolanding/{environment}/{tenantId}/{draftId}/notifications/smtp/{connectionId}` with the existing scope, purpose, connection, and enabled tags plus `zoolanding:smtp-account-isolation-id` and `zoolanding:smtp-credential-isolation-id`. Set `SmtpTestSharedAccountClaimHash` to the lowercase SHA-256 of the approved opaque test account-isolation ID, and set `SmtpActivationCallerArns` only to the approved operator role identities—not Notifications or general service roles. Do not place raw isolation IDs or real role ARNs in templates, drafts, logs, or versioned files.

Every binding must select its Accounts v1 strategy explicitly. Accounts v2 activation remains blocked until the approved Mexico Sandbox proof covers the required capabilities and topology. Account ownership, provider account references, routing claims, controller properties, tokens, and credential values never enter draft configuration or browser responses.

Production subscription mutations require an exact server-owned tax approval before any Stripe access. The approval binds environment, tenant, draft, domain, connection, hashed provider account, mode, command revision, and approval revision/hash. Missing, stale, corrupt, cross-account, or wrong-mode approval returns `needs_review` without a provider call. Test code may inject its verifier; production composition may not.

Hosted Checkout and Customer Portal URLs are validated ephemeral `no-store` responses. They are not persisted in Registry mappings, receipts, events, logs, examples, or documentation.

## Runtime dependencies

- `boto3==1.39.13` matches the approved Zoolanding Python service baseline.
- `stripe==15.3.1` is the official Stripe Python SDK and the sole new runtime dependency. It is isolated behind the Stripe adapter; provider-neutral domain code does not import it.
- `PyYAML==6.0.2` is confined to `requirements-dev.txt` for template contract tests and is not packaged into Lambda runtime dependencies.

## Local verification

The current Phase 6 tree passes 282 unit/contract tests, dependency audit, Python compilation, SAM lint, and an uncached SAM build of all 24 functions. No deployment, SMTP send, secret read, or provider-backed proof is claimed.

```powershell
python -m pip install --requirement requirements-dev.txt
python -m unittest discover -s tests -p "test_*.py" -v
python -m pip_audit --requirement requirements-dev.txt
python -m compileall -q src tests
sam validate --lint
sam build --no-cached
```

A local SAM build requires the official Python 3.13 installation, including its `DLLs` and `Scripts` directories, ahead of Windows app aliases on `PATH`. Remove the local `uv` shim from `PATH` for this command if it shadows that interpreter; the declared production runtime is not relaxed to match another workstation interpreter.

## Closed rollout boundary

- Phase 5 bulk subscription migration and the Phase 6 SMTP activation/resolution boundary are locally implemented. Provider-backed behavior and operational scale remain unproven until deployment and controlled environment testing.
- Phase 8 owns AWS deployment, exact cross-service IAM identities, queues/tables/topics, alarms, quotas, environment credentials, webhook configuration, and provider-backed end-to-end/failure testing.
- Phase 9 owns per-draft pilot configuration, production tax/live approval, and activation.

There is no AWS `dev` stack, deployment workflow/profile, live credential, connected account, webhook endpoint, provider call, browser QA, end-to-end Stripe proof, or pilot activation recorded through Phase 5. Deployment and live activation remain NO-GO until their later gates are explicitly approved and verified.
