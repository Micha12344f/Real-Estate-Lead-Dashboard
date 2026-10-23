"""Verify mode/data isolation on the local CI simulation stack, never a live deploy."""
import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.request
from urllib.parse import urlsplit

DASH = "http://127.0.0.1:3002/sandbox/api/"
PREFIX = os.environ.get("TVE_E2E_PG_PREFIX", "tve-pg-")
TABLES = {"contacts": "contacts", "enquiries": "enquiries", "responses": "questionnaire_responses",
          "tokens": "tokens", "settings": "settings"}


def request(path, body=None, method=None, headers=None):
    req = urllib.request.Request(DASH + path, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=45) as response:
        return json.load(response)


def rejected(path, status, body=None, headers=None, method=None):
    try:
        request(path, body, headers=headers, method=method)
    except urllib.error.HTTPError as error:
        assert error.code == status, f"Expected HTTP {status}, got {error.code}"
    else:
        raise AssertionError(f"Expected HTTP {status}")


def fingerprints():
    result = {}
    for domain, table in TABLES.items():
        where = " WHERE key <> 'TVE_SANDBOX_MODE'" if domain == "settings" else ""
        sql = f"SELECT md5(COALESCE(string_agg(row_to_json(t)::text, '' ORDER BY row_to_json(t)::text), '')) FROM (SELECT * FROM {table}{where}) t"
        # Data/credentials stay inside Postgres; only a digest leaves the container.
        command = f'exec psql -U "$POSTGRES_USER" -d {domain} -At -v ON_ERROR_STOP=1 -c "{sql}"'
        output = subprocess.check_output(["docker", "exec", PREFIX + domain, "sh", "-c", command], stderr=subprocess.DEVNULL)
        result[domain] = hashlib.sha256(output).hexdigest()
    return result


def main():
    mode = request("settings/mode")
    assert mode["sandbox"] and mode["environmentMode"] == "sandbox", "Run only on a sandbox-bootstrapped simulation stack"
    before = fingerprints()
    added = request("settings/sandbox/seed", {})
    assert 0 <= added["added"] <= 6
    leads = request("leads")["leads"]
    assert len([row for row in leads if row["email"].startswith("demo.") and row["email"].endswith("@sandbox.invalid")]) == 6
    test_lead = next(row for row in leads if row["email"] == "demo.completed@sandbox.invalid")
    booking_token = urlsplit(test_lead["links"]["booking"]).path.rsplit("/", 1)[-1]
    try:
        request("settings/mode", {"sandbox": False}, method="PUT")
        # A credential-free CI deployment has no live CRM catalogue. Check the
        # live contact DB directly rather than attempting provider-dependent UI reads.
        count = subprocess.check_output(["docker", "exec", PREFIX + "contacts", "sh", "-c",
            'exec psql -U "$POSTGRES_USER" -d contacts -At -c "SELECT count(*) FROM contacts WHERE email LIKE \'demo.%@sandbox.invalid\'"'],
            stderr=subprocess.DEVNULL)
        assert int(count) == 0
        security = next(group for group in request("settings")["groups"] if group["id"] == "security")
        assert next(field for field in security["fields"] if field["key"] == "TVE_TOKEN_KEY")["environment"] == "production"
        rejected("settings/sandbox/seed", 409, {})
        rejected("actions/assign-property", 409, {"contact_id": test_lead["id"], "property_id": test_lead["property_id"]},
                 headers={"X-TVE-Mode": "sandbox"})
        try:
            urllib.request.urlopen("http://127.0.0.1:3000/booking/api/availability?token=" + booking_token, timeout=30)
        except urllib.error.HTTPError as error:
            assert error.code == 401
        else:
            raise AssertionError("Sandbox token was accepted in Production")
    finally:
        request("settings/mode", {"sandbox": True}, method="PUT")
    assert request("settings/sandbox/seed", {})["added"] == 0
    restored = next(row for row in request("leads")["leads"] if row["id"] == test_lead["id"])
    assert restored["email"] == test_lead["email"] and restored["status"] == test_lead["status"]
    rejected("settings/ACQUAINT_API_KEY", 409, {"value": "no-live-edit"}, method="PUT")
    assert fingerprints() == before, "Sandbox activity changed production journey/settings data"
    print(json.dumps({"status": "passed", "checks": ["separate Postgres data", "six seeded UI stages",
          "idempotent seeding", "production seed rejection", "stale dashboard rejection",
          "cross-mode token rejection", "sandbox data survives toggling", "production fingerprints unchanged"]}, indent=2))


if __name__ == "__main__":
    main()
