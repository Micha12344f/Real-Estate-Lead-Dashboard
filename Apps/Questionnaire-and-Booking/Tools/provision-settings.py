"""Provision the local vault bootstrap without printing or replacing secrets.

Run after installing backend requirements, before recreating the Python services.
Existing valid master keys are preserved. Losing that key loses encrypted values.
"""
from pathlib import Path
import re

from cryptography.fernet import Fernet


def main():
    path = Path(__file__).resolve().parents[1] / "backend" / ".env"
    if not path.is_file():
        raise SystemExit("Create backend/.env from .env.example first.")
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(r"^TVE_SETTINGS_MASTER_KEY=(.*)$", re.MULTILINE)
    match = pattern.search(text)
    value = match.group(1).strip().strip("\"'") if match else ""
    if value and not value.startswith("<"):
        try:
            Fernet(value.encode())
        except (ValueError, TypeError):
            raise SystemExit("Existing master key is invalid; correct it before provisioning.")
        print("Existing vault master key preserved.")
    else:
        assignment = "TVE_SETTINGS_MASTER_KEY=" + Fernet.generate_key().decode()
        text = pattern.sub(lambda _: assignment, text) if match else text.rstrip() + "\n\n# Credential vault\n" + assignment + "\n"
        print("Vault master key provisioned in gitignored backend/.env.")
    if not re.search(r"^TVE_WEBHOOK_SECRET=", text, re.MULTILINE):
        text = text.rstrip() + "\nTVE_WEBHOOK_SECRET=sandbox-webhook-secret\n"
    text = re.sub(r"^TVE_SETTINGS_TTL_SECONDS=.*$", "TVE_SETTINGS_TTL_SECONDS=5", text, flags=re.MULTILINE)
    if not re.search(r"^TVE_SETTINGS_TTL_SECONDS=", text, re.MULTILINE):
        text = text.rstrip() + "\nTVE_SETTINGS_TTL_SECONDS=5\n"
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
