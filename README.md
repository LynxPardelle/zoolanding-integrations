# Zoolanding Integrations

Generic, server-only integration connection and provider-mapping foundation for Zoolanding drafts.

The current local phase implements immutable published-policy resolution, provider-neutral connection/binding contracts, retained DynamoDB boundaries, fail-closed Stream boundaries, and the normalized Integration Events topic. It has no API route, live credential, provider call, AWS deployment, or AWS `dev` environment.

## Runtime dependencies

- `boto3==1.39.13` matches the approved Zoolanding Python service baseline.
- `stripe==15.3.1` is the official Stripe Python SDK and the sole new runtime dependency. It will be used only inside the later Stripe adapter; provider-neutral domain code does not import it.

## Local verification

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
python -m compileall -q src tests
sam validate --lint
```

The Stream functions deliberately return every received record as a partial-batch failure. TASK-043/TASK-044 must replace that fail-closed boundary with verified webhook/outbox processing before any deployment.
