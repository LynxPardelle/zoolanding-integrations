"""Import every SAM handler from its exact built Lambda code root."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
_MODULE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", re.ASCII
)
_LOGICAL_ID = re.compile(r"[A-Za-z][A-Za-z0-9]{0,254}", re.ASCII)
_CREDENTIAL_ENVIRONMENT = {
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
}


class BuildVerificationError(RuntimeError):
    pass


def verify() -> int:
    template_path = ROOT / "template.yaml"
    build_root = ROOT / ".aws-sam" / "build"
    if not (build_root / "template.yaml").is_file():
        raise BuildVerificationError("built template is unavailable")
    with template_path.open(encoding="utf-8") as handle:
        template = yaml.safe_load(handle)
    resources = template.get("Resources") if isinstance(template, dict) else None
    if not isinstance(resources, dict):
        raise BuildVerificationError("source template is invalid")

    checked = 0
    for logical_id, resource in resources.items():
        if not isinstance(resource, dict) or resource.get("Type") != "AWS::Serverless::Function":
            continue
        properties = resource.get("Properties")
        handler_value = properties.get("Handler") if isinstance(properties, dict) else None
        if (
            type(logical_id) is not str
            or _LOGICAL_ID.fullmatch(logical_id) is None
            or type(handler_value) is not str
            or "." not in handler_value
        ):
            raise BuildVerificationError("function definition is invalid")
        module = handler_value.rsplit(".", 1)[0]
        if _MODULE.fullmatch(module) is None:
            raise BuildVerificationError("handler module is invalid")
        code_root = build_root / logical_id
        if not code_root.is_dir():
            raise BuildVerificationError(f"build root is missing for {logical_id}")
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in _CREDENTIAL_ENVIRONMENT
        }
        environment["AWS_EC2_METADATA_DISABLED"] = "true"
        environment["PYTHONPATH"] = str(code_root)
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import importlib,sys; importlib.import_module(sys.argv[1])",
                    module,
                ],
                cwd=code_root,
                env=environment,
                capture_output=True,
                check=False,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            raise BuildVerificationError(
                f"handler import could not run for {logical_id}"
            ) from None
        if result.returncode != 0:
            raise BuildVerificationError(f"handler import failed for {logical_id}")
        checked += 1
    if checked < 1:
        raise BuildVerificationError("no Lambda handlers were found")
    return checked


def main() -> int:
    try:
        count = verify()
    except BuildVerificationError as error:
        print(f"build_verification=failed reason={error}", file=sys.stderr)
        return 1
    print(f"built_handler_imports=ok count={count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
