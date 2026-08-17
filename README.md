# Zoolanding Integrations

Generic, server-only connection, provider-mapping, confirmed-provider-event, bulk subscription-migration, and SMTP connection-metadata service for Zoolanding drafts. Phases 5 and 6 plus the Phase 8 deployment-readiness surface are implemented locally; nothing in this repository is deployed or provider-activated.

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

## Phase 8 deployment readiness

CI now runs on every pushed branch and every pull request. Promotion remains `dev -> test -> main`; the only deployment profiles and workflows are `test` and `production`. Each deployment workflow proves the exact two-parent merged pull request before building, then repeats that proof before requesting AWS credentials. The validated SAM build is transferred to the deployment job with a closed SHA-256 manifest. OIDC is available only to the environment-protected deployment job; CI and validation jobs have read-only GitHub permissions and no AWS credential step. There is no AWS `dev` profile or workflow.

The deployment fails closed until these existing Config and Auth SSM parameters contain the real cross-service resource names for the target environment:

- `/zoolanding/{environment}/config/registry-table-name`
- `/zoolanding/{environment}/config/payload-bucket-name`
- `/zoolanding/{environment}/auth/session-table-name`
- `/zoolanding/{environment}/auth/user-state-table-name`

The OIDC deployment role needs `ssm:GetParameter` for those four literal parameter names and, after bootstrap, `/zoolanding/{environment}/services/commerce/integrations-caller-role-arns` plus `/zoolanding/{environment}/services/notifications/smtp-worker-role-arn`. The workflow rejects malformed values and proves that the configured caller list contains all four exact Commerce callers and the Notifications worker before CloudFormation. The CloudFormation execution role needs `ssm:GetParameters` only for the four Config/Auth names resolved by the template. Neither role receives a wildcard SSM read, and Config/Auth writers must not be shared with the deployment roles so the values cannot change across the validation/deployment boundary.

The Integrations stack publishes only its safe composition identifiers at `/zoolanding/{environment}/services/integrations/api-id` and `/zoolanding/{environment}/topics/integration-events-arn`. It does not publish secrets, claim hashes, credentials, provider payloads, or customer data.

Each protected GitHub environment must provide `AWS_ROLE_ARN`, `AWS_CLOUDFORMATION_ROLE_ARN`, `SMTP_ACTIVATION_CALLER_ARNS`, `ALARM_TOPIC_ARN`, `STRIPE_WEBHOOK_RATE_LIMIT`, `STRIPE_WEBHOOK_BURST_LIMIT`, and `STRIPE_WEBHOOK_RESERVED_CONCURRENCY` as environment variables, plus `SMTP_TEST_SHARED_ACCOUNT_CLAIM_HASH` as an environment secret. `INTERNAL_CALLER_ARNS` is the one intentionally optional bootstrap variable: an empty value is a disabled caller set, so every general internal request remains forbidden. Once enabled, both caller lists accept only exact comma-separated IAM role ARNs. Runtime STS assumed-role identities are normalized back to their exact configured IAM role ARN; users, federated users, wildcards, malformed sessions, and partially invalid lists are rejected. Do not put any real value from these settings in this repository.

After OIDC, each workflow derives the current partition and account from `sts:GetCallerIdentity` and requires the deployment role, CloudFormation execution role, alarm topic, and every configured caller role to use the same AWS partition and account as the OIDC session. The `ALARM_TOPIC_ARN` region must equal `AWS_REGION`, the default AWS region and SAM deployment region must match it, and the partition must support that region family. This validation fails with a generic error and does not print those values. There is no independently configurable AWS endpoint in this deployment surface.

Resolve the cross-service dependency without a permissive placeholder in two passes. First deploy Integrations with the disabled caller set so it can publish the API and event-topic identifiers while all general internal routes remain unusable. The deploy workflow proves with CloudFormation that the target stack does not yet exist before accepting that empty set; every update to an existing stack requires a nonempty exact caller list. Commerce and Notifications publish their exact Integrations-caller role identifiers during their own deployments. Then assemble only those published role ARNs into `INTERNAL_CALLER_ARNS`, redeploy Integrations, grant each caller only its literal `execute-api:Invoke` routes, and run the signed smokes. If the Config, Auth, alarm topic, OIDC, CloudFormation execution-role, or second-pass caller inputs do not exist, activation remains blocked. A missing dependency is never replaced by `*`, a temporary principal, or an invented ARN.

The template sends API 5xx, method-scoped webhook API 4xx, webhook Lambda errors/reserved-concurrency throttles, migration worker errors/throttles, signed-webhook age, signature failures, test/live mismatches, migration queue age/backlog, and migration dead-letter age/depth to the exact `ALARM_TOPIC_ARN`. The webhook 4xx signal includes validation/authentication failures and API Gateway 429 responses; it is deliberately not labeled as an exact throttle count. Custom metrics use the closed `Zoolanding/Integrations` namespace with only the environment dimension; no draft, account, email, payload, or credential data is emitted.

The public Stripe webhook has a route-specific API Gateway rate/burst target and a Lambda reserved-concurrency cap. Deployment values are required rather than guessed: the code-owned ceilings are 100 requests/second, burst 200, and concurrency 20, but they are safety bounds—not recommended operating values. Test activation remains blocked until Stripe retry behavior, webhook bursts, downstream latency, account concurrency, and cost are measured and the lower environment-specific values are approved. API Gateway throttling is best-effort, so signature/replay checks and the existing alarms remain mandatory; this does not add WAF.

The three browser routes also have pre-auth cost ceilings: each API method is limited to 10 requests/second with burst 20, and each backing Lambda reserves at most five concurrent executions. These are conservative code-owned safety limits, not a throughput claim. They bound anonymous policy/Auth reads before authorization; later load evidence must justify any reviewed increase.

The outgoing Integration Events topic uses the AWS-managed SNS encryption key. Both DynamoDB Stream failure destinations alarm on message arrival and on records older than five minutes, in addition to the migration queue alarms. A confirmed human-operated subscriber on `ALARM_TOPIC_ARN` remains a deployment gate.

After a controlled deployment and exact caller-policy bootstrap, the read-only readiness smoke signs `POST /internal/v1/integrations/connection-resolve` through the default AWS credential chain. It never accepts credentials on the command line and prints only status, attempt count, the closed classifications `ready`, `missing_input`, `auth_failure`, `propagation_delay`, `configuration_failure`, or `provider_failure`, plus a redacted evidence envelope. Every result includes `environment`, derived only from a fully validated API stage (`test` or `production`, otherwise `null`), and integer `observedAtEpoch`, captured exactly once from the current or injected clock. This lets an external aggregator reject mixed-environment or stale evidence without exposing the URL, domain, connection, AWS identity, request, or response.

```powershell
$env:ZLP_INTEGRATIONS_SMOKE_API_URL = 'https://{api-id}.execute-api.{region}.amazonaws.com/test'
$env:ZLP_INTEGRATIONS_SMOKE_TENANT_ID = '{safe-tenant-id}'
$env:ZLP_INTEGRATIONS_SMOKE_DRAFT_ID = '{safe-draft-id}'
$env:ZLP_INTEGRATIONS_SMOKE_DOMAIN = '{canonical-domain}'
$env:ZLP_INTEGRATIONS_SMOKE_CONNECTION_ID = '{active-smtp-connection-id}'
$env:AWS_REGION = '{region}'
python tools/integration_platform_readiness_smoke.py
```

An optional `ZLP_INTEGRATIONS_SMOKE_PROPAGATION_UNTIL_EPOCH` may classify an initial 404 as propagation delay for at most 15 minutes. It never turns an authentication or server error into success.

No AWS deployment was performed as part of this implementation. No provider endpoint, secret, OIDC role, alarm destination, or cross-service resource has been claimed as present or working.

## Runtime dependencies

- `boto3==1.39.13` matches the approved Zoolanding Python service baseline.
- `stripe==15.3.1` is the official Stripe Python SDK and the sole new runtime dependency. It is isolated behind the Stripe adapter; provider-neutral domain code does not import it.
- `PyYAML==6.0.2` is confined to `requirements-dev.txt` for template contract tests and is not packaged into Lambda runtime dependencies.

## Local verification

The Phase 8 readiness tree passes 312 unit/contract tests, dependency audit, Python compilation, workflow lint, SAM lint, an uncached build, and import verification for all 24 Lambda handlers. CI also scans full Git history with the commit-pinned Gitleaks action and the repository's narrow synthetic-test allowlist. No deployment, SMTP send, secret read, or provider-backed proof is claimed.

```powershell
python -m pip install --requirement requirements-dev.txt
python -m unittest discover -s tests -p "test_*.py" -v
python -m pip_audit --requirement requirements.txt
python -m compileall -q src tools tests
sam validate --lint
sam build --no-cached
python tools/verify_sam_build.py
```

A local SAM build requires the official Python 3.13 installation, including its `DLLs` and `Scripts` directories, ahead of Windows app aliases on `PATH`. Remove the local `uv` shim from `PATH` for this command if it shadows that interpreter; the declared production runtime is not relaxed to match another workstation interpreter.

## Closed rollout boundary

- Phase 5 bulk subscription migration and the Phase 6 SMTP activation/resolution boundary are locally implemented. Provider-backed behavior and operational scale remain unproven until deployment and controlled environment testing.
- Phase 8 now provides the local CI/CD, exact cross-service input, least-privilege IAM, alarm, metric, and redacted-smoke surfaces. Actual AWS provisioning, environment credentials, webhook configuration, quotas, and provider-backed end-to-end/failure proof remain deployment gates.
- Phase 9 owns per-draft pilot configuration, production tax/live approval, and activation.

There is no AWS `dev` stack, deployment workflow/profile, live credential, connected account, webhook endpoint, provider call, browser QA, end-to-end Stripe proof, or pilot activation recorded. Deployment and live activation remain NO-GO until their later gates are explicitly approved and verified.
