# Zoolanding Integrations

Generic, server-only integration connection and provider-mapping foundation for Zoolanding drafts.

The current local phase implements immutable published-policy resolution, provider-neutral connection/binding contracts, Auth Admin authorization, retained DynamoDB boundaries, and separate browser and AWS_IAM API seams. Browser reads/actions and internal connection registration/resolution are runtime-composed. Stripe onboarding is dependency-injected and provider command/migration/Stream entrypoints remain fail closed until their later implementation and rollout gates. There is no live credential, provider call, deployment configuration, or AWS `dev` environment in this repository.

## Runtime dependencies

- `boto3==1.39.13` matches the approved Zoolanding Python service baseline.
- `stripe==15.3.1` is the official Stripe Python SDK and the sole new runtime dependency. It is isolated behind the Stripe adapter; provider-neutral domain code does not import it.

## Local verification

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python -m compileall -q src tests
sam validate --lint
```

The Stream functions deliberately return every received record as a partial-batch failure. Provider command and migration functions also return a typed unavailable response. Their implementation tasks must replace these fail-closed boundaries before any deployment that exposes them. A local SAM build additionally requires a Python 3.13 runtime on `PATH`; validation does not relax the declared production runtime to match an older workstation interpreter.
