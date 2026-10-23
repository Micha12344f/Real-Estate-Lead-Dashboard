"""Production replay guarantees using a real private state file and fake IMAP."""
import asyncio
import json
import logging
import os
import stat
from email.message import EmailMessage

import pytest

from app.services.shared import database
from app.workers import imap_checkpoint, imap_intake


def mail(uid, *, message_id=None, enquiry=True):
    message = EmailMessage()
    message["From"] = "Rightmove <autoresponder@rightmove.com>"
    message["Subject"] = "Lettings enquiry: 2 High Street" if enquiry else "Newsletter"
    message["Message-ID"] = message_id or f"<message-{uid}@example.test>"
    message.set_content(
        "Hi, Jane has enquired about this property\n\n2 High Street, Reading, RG1 1AA\n\u00a31,250 pcm\n1 bed\n\n"
        "Jane Applicant\njane@example.test\n07700 900123\nAPPLICANT WOULD LIKE\n    Book a viewing\n"
        if enquiry else "Latest news")
    return message.as_bytes()


class Mailbox:
    def __init__(self):
        self.validity = 777
        self.next_uid = 104
        self.messages = {uid: mail(uid) for uid in range(1, 104)}
        self.calls = []
        self.fetch_failure = None
        self.metadata_override = {}

    def login(self, *args):
        return "OK", []

    def select(self, mailbox, readonly=False):
        self.calls.append(("select", mailbox, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def response(self, name):
        value = self.metadata_override.get(name, self.validity if name == "UIDVALIDITY" else self.next_uid)
        return name, [value if isinstance(value, bytes) or value is None else str(value).encode()]

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command == "search":
            floor = int(args[-1].split(":")[0])
            # IMAP n:* can return the last UID even when n exceeds it.
            uids = [uid for uid in self.messages if uid >= floor]
            if not uids and self.messages:
                uids = [max(self.messages)]
            return "OK", [" ".join(map(str, reversed(uids))).encode()]
        assert command == "fetch"
        assert args[1] == "(BODY.PEEK[])"
        uid = int(args[0])
        if uid == self.fetch_failure:
            return "NO", []
        return "OK", [(b"BODY[]", self.messages[uid])]

    def logout(self):
        self.calls.append(("logout",))

    def arrive(self, uid, **kwargs):
        self.messages[uid] = mail(uid, **kwargs)
        self.next_uid = uid + 1


@pytest.fixture
def worker(monkeypatch, tmp_path, caplog):
    # Application logging may disable propagation on the tve.sandbox parent.
    # Capture this worker directly so full-suite imports cannot hide its logs.
    monkeypatch.setattr(imap_intake.logger, "handlers", [caplog.handler])
    monkeypatch.setattr(imap_intake.logger, "propagate", False)
    monkeypatch.setattr(imap_intake.logger, "level", logging.INFO)
    monkeypatch.setenv("TVE_IMAP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("TVE_IMAP_MAX_MESSAGES_PER_POLL", "25")
    config = imap_intake._build_imap_config({
        "TVE_IMAP_HOST": "imap.example.test", "TVE_IMAP_USERNAME": "private-mail-user",
        "TVE_IMAP_PASSWORD": "private-password", "TVE_IMAP_MAILBOX": "INBOX"})
    mailbox = Mailbox()
    opened, committed = [], []

    def connect(*args, **kwargs):
        assert kwargs["ssl_context"] is not None
        opened.append(True)
        return mailbox

    async def ingest(pool, raw, **kwargs):
        committed.append(kwargs)
        return 7

    monkeypatch.setattr(imap_intake.imaplib, "IMAP4_SSL", connect)
    monkeypatch.setattr(imap_intake, "ingest_email", ingest)
    return config, mailbox, committed, opened, tmp_path


def state(worker):
    return json.loads((worker[-1] / "checkpoint.json").read_text())


def cursor(worker, config=None):
    return state(worker)["mailboxes"][imap_intake._cursor_identity(config or worker[0])]


@pytest.mark.asyncio
async def test_first_connection_baselines_without_fetch_or_intake(worker, caplog):
    config, mailbox, committed, opened, directory = worker
    with caplog.at_level("INFO"):
        result = await imap_intake.poll_once(object(), config)
    assert result == {"fetched": 0, "new_enquiries": 0, "last_uid": 103}
    assert cursor(worker) == {"uid_validity": 777, "last_uid": 103}
    assert not committed
    assert not any(call[0] in {"fetch", "search"} for call in mailbox.calls)
    assert "first_connection" in caplog.text
    for private in (config["username"], config["password"], config["host"], "jane@example.test"):
        assert private not in caplog.text
        assert private not in (directory / "checkpoint.json").read_text()
    if os.name != "nt":
        assert stat.S_IMODE((directory / "checkpoint.json").stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_restart_resumes_completed_uid_with_actual_validity_and_peek(worker):
    config, mailbox, committed, _, _ = worker
    await imap_intake.poll_once(object(), config)
    mailbox.arrive(104)
    mailbox.arrive(105)
    result = await imap_intake.poll_once(object(), config)
    assert result == {"fetched": 2, "new_enquiries": 2, "last_uid": 105}
    assert [row["uid"] for row in committed] == [104, 105]
    assert {row["uid_validity"] for row in committed} == {777}
    # Every poll reopens the file, so this has the same state semantics as restart.
    assert (await imap_intake.poll_once(object(), dict(config)))["fetched"] == 0
    mailbox.arrive(106)
    await imap_intake.poll_once(object(), config)
    assert [row["uid"] for row in committed] == [104, 105, 106]
    assert all(call[2] is True for call in mailbox.calls if call[0] == "select")
    assert all(call[2] == "(BODY.PEEK[])" for call in mailbox.calls if call[0] == "fetch")
    assert not any(call[0] in {"store", "copy", "move", "expunge", "delete"} for call in mailbox.calls)


@pytest.mark.asyncio
async def test_uidvalidity_rollover_baselines_new_mailbox_and_does_not_fetch_history(worker, caplog):
    config, mailbox, committed, _, _ = worker
    await imap_intake.poll_once(object(), config)
    mailbox.arrive(104)
    await imap_intake.poll_once(object(), config)
    mailbox.validity = 888
    mailbox.next_uid = 8
    mailbox.messages = {uid: mail(uid) for uid in range(1, 8)}
    mailbox.calls.clear()
    with caplog.at_level("INFO"):
        assert (await imap_intake.poll_once(object(), config))["last_uid"] == 7
    assert "uidvalidity_changed" in caplog.text
    assert not any(call[0] in {"fetch", "search"} for call in mailbox.calls)
    mailbox.arrive(8)
    await imap_intake.poll_once(object(), config)
    assert [(row["uid_validity"], row["uid"]) for row in committed] == [(777, 104), (888, 8)]


@pytest.mark.asyncio
async def test_transient_intake_failure_stops_at_failed_uid_and_retries_after_restart(worker, monkeypatch):
    config, mailbox, committed, _, _ = worker
    await imap_intake.poll_once(object(), config)
    for uid in (104, 105, 106):
        mailbox.arrive(uid)
    attempts = []

    async def ingest(pool, raw, **kwargs):
        attempts.append(kwargs["uid"])
        if kwargs["uid"] == 105 and attempts.count(105) == 1:
            raise RuntimeError("transient")
        committed.append(kwargs)
        return 7

    monkeypatch.setattr(imap_intake, "ingest_email", ingest)
    with pytest.raises(RuntimeError):
        await imap_intake.poll_once(object(), config)
    assert cursor(worker)["last_uid"] == 104
    assert attempts == [104, 105]
    await imap_intake.poll_once(object(), config)
    assert attempts == [104, 105, 105, 106]
    assert cursor(worker)["last_uid"] == 106


@pytest.mark.asyncio
async def test_transient_fetch_failure_does_not_advance_any_uid(worker):
    config, mailbox, committed, _, _ = worker
    await imap_intake.poll_once(object(), config)
    mailbox.arrive(104)
    mailbox.arrive(105)
    mailbox.fetch_failure = 105
    with pytest.raises(RuntimeError):
        await imap_intake.poll_once(object(), config)
    assert cursor(worker)["last_uid"] == 103
    assert not committed
    mailbox.fetch_failure = None
    await imap_intake.poll_once(object(), config)
    assert [row["uid"] for row in committed] == [104, 105]


@pytest.mark.asyncio
async def test_config_change_preserves_identity_cursors_and_durable_cross_identity_dedupe(worker):
    config, mailbox, committed, _, _ = worker
    await imap_intake.poll_once(object(), config)
    mailbox.arrive(104, message_id="<shared@example.test>")
    await imap_intake.poll_once(object(), config)
    changed = dict(config, mailbox="Enquiries")
    await imap_intake.poll_once(object(), changed)  # new identity baseline at 104
    mailbox.arrive(105, message_id="<shared@example.test>")
    assert (await imap_intake.poll_once(object(), changed))["new_enquiries"] == 0
    mailbox.arrive(106)
    # Returning to the original identity resumes 104; shared Message-ID is skipped.
    assert (await imap_intake.poll_once(object(), config))["new_enquiries"] == 1
    assert [row["uid"] for row in committed] == [104, 106]
    assert cursor(worker)["last_uid"] == 106
    assert cursor(worker, changed)["last_uid"] == 105
    assert len(state(worker)["mailboxes"]) == 2
    assert "shared@example.test" not in json.dumps(state(worker))
    assert imap_intake._cursor_identity(dict(config, password="rotated")) == imap_intake._cursor_identity(config)
    assert imap_intake._cursor_identity(dict(config, port=1993)) != imap_intake._cursor_identity(config)


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["{broken", "", "{}", '{"version":2,"mailboxes":{},"dedupe":[]}',
                               '{"version":1,"version":1,"mailboxes":{},"dedupe":[]}',
                               '{"version":1,"mailboxes":{"bad":{"uid_validity":1,"last_uid":0}},"dedupe":[]}'])
async def test_corrupt_state_fails_closed_before_provider_connection(worker, raw):
    config, _, committed, opened, directory = worker
    (directory / "checkpoint.json").write_text(raw)
    with pytest.raises(imap_checkpoint.CheckpointError):
        await imap_intake.poll_once(object(), config)
    assert not opened and not committed
    assert (directory / "checkpoint.json").read_text() == raw


@pytest.mark.asyncio
async def test_unavailable_directory_or_atomic_write_fails_closed(worker, monkeypatch):
    config, _, committed, opened, directory = worker
    monkeypatch.setenv("TVE_IMAP_STATE_DIR", str(directory / "missing"))
    with pytest.raises(imap_checkpoint.CheckpointError):
        await imap_intake.poll_once(object(), config)
    monkeypatch.setenv("TVE_IMAP_STATE_DIR", str(directory))

    def unavailable(*args):
        raise PermissionError("private-path")

    monkeypatch.setattr(imap_checkpoint.os, "replace", unavailable)
    with pytest.raises(imap_checkpoint.CheckpointError, match="imap_checkpoint_write_failed"):
        await imap_intake.poll_once(object(), config)
    assert not opened and not committed
    assert not (directory / ".checkpoint.tmp").exists()


@pytest.mark.asyncio
async def test_checkpoint_write_failure_after_commit_retries_uid_and_blocks_later_uids(worker, monkeypatch):
    config, mailbox, committed, _, directory = worker
    await imap_intake.poll_once(object(), config)
    mailbox.arrive(104)
    mailbox.arrive(105)
    original = imap_checkpoint.os.replace

    def fail_after_commit(*args):
        if committed:
            raise OSError("disk failure")
        return original(*args)

    monkeypatch.setattr(imap_checkpoint.os, "replace", fail_after_commit)
    with pytest.raises(imap_checkpoint.CheckpointError):
        await imap_intake.poll_once(object(), config)
    assert [row["uid"] for row in committed] == [104]
    assert cursor(worker)["last_uid"] == 103
    assert not state(worker)["dedupe"]
    assert not (directory / ".checkpoint.tmp").exists()
    monkeypatch.setattr(imap_checkpoint.os, "replace", original)
    await imap_intake.poll_once(object(), config)
    assert [row["uid"] for row in committed] == [104, 104, 105]
    assert cursor(worker)["last_uid"] == 105


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["parser", "uncommitted"])
async def test_parser_or_uncommitted_intake_cannot_advance(worker, monkeypatch, failure):
    config, mailbox, committed, _, _ = worker
    await imap_intake.poll_once(object(), config)
    mailbox.arrive(104)
    mailbox.arrive(105)

    def bad_parse(*args):
        raise ValueError("parse failure")

    async def no_commit(*args, **kwargs):
        return None

    original_parse = imap_intake.parse_email
    original_ingest = imap_intake.ingest_email
    monkeypatch.setattr(imap_intake, "parse_email", bad_parse if failure == "parser" else original_parse)
    monkeypatch.setattr(imap_intake, "ingest_email", no_commit if failure == "uncommitted" else original_ingest)
    with pytest.raises((ValueError, RuntimeError)):
        await imap_intake.poll_once(object(), config)
    assert cursor(worker)["last_uid"] == 103
    assert not committed
    monkeypatch.setattr(imap_intake, "parse_email", original_parse)
    monkeypatch.setattr(imap_intake, "ingest_email", original_ingest)
    await imap_intake.poll_once(object(), config)
    assert [row["uid"] for row in committed] == [104, 105]


@pytest.mark.asyncio
async def test_empty_mailbox_baseline_and_missing_metadata_fail_closed(worker):
    config, mailbox, committed, _, _ = worker
    mailbox.messages = {}
    mailbox.next_uid = 1
    assert (await imap_intake.poll_once(object(), config))["last_uid"] == 0
    mailbox.arrive(1)
    mailbox.metadata_override["UIDVALIDITY"] = None
    with pytest.raises(imap_checkpoint.CheckpointError):
        await imap_intake.poll_once(object(), config)
    assert not committed
    mailbox.metadata_override.clear()
    await imap_intake.poll_once(object(), config)
    assert [row["uid"] for row in committed] == [1]


@pytest.mark.asyncio
async def test_non_enquiry_completion_and_poll_bound(worker, monkeypatch):
    config, mailbox, committed, _, _ = worker
    await imap_intake.poll_once(object(), config)
    mailbox.arrive(104, enquiry=False)
    mailbox.arrive(105)
    monkeypatch.setenv("TVE_IMAP_MAX_MESSAGES_PER_POLL", "1")
    result = await imap_intake.poll_once(object(), config)
    assert result == {"fetched": 1, "new_enquiries": 0, "last_uid": 104}
    assert not committed
    await imap_intake.poll_once(object(), config)
    assert [row["uid"] for row in committed] == [105]


def test_bounded_dedupe_and_identity_capacity_preserve_existing_cursors(tmp_path, monkeypatch):
    monkeypatch.setattr(imap_checkpoint, "MAX_DEDUPE_KEYS", 3)
    monkeypatch.setattr(imap_checkpoint, "MAX_IDENTITIES", 2)
    with imap_checkpoint.CheckpointStore(tmp_path) as checkpoint:
        first, second, third = (imap_checkpoint.dedupe_hash(key) for key in ("first", "second", "third"))
        checkpoint.prepare(first, 777, 1)
        for uid in range(1, 6):
            checkpoint.complete(first, uid, f"message-{uid}")
        checkpoint.prepare(second, 777, 10)
        with pytest.raises(imap_checkpoint.CheckpointError):
            checkpoint.prepare(third, 777, 20)
    with imap_checkpoint.CheckpointStore(tmp_path) as checkpoint:
        assert checkpoint.state["mailboxes"][first]["last_uid"] == 5
        assert len(checkpoint.state["dedupe"]) == 3
        assert not checkpoint.contains("message-2")
        assert checkpoint.contains("message-3")
        assert checkpoint.prepare(first, 777, 6) == (5, None)


def test_second_worker_cannot_acquire_poll_lock(tmp_path):
    with imap_checkpoint.CheckpointStore(tmp_path):
        with pytest.raises(imap_checkpoint.CheckpointError):
            with imap_checkpoint.CheckpointStore(tmp_path):
                pytest.fail("second worker acquired the checkpoint lock")


@pytest.mark.asyncio
async def test_checkpoint_failure_pauses_followups_and_logs_only_error_type(worker, monkeypatch, caplog):
    config, _, _, _, directory = worker
    (directory / "checkpoint.json").write_text("private-corrupt-data")
    monkeypatch.delenv("SANDBOX", raising=False)
    monkeypatch.setenv("sandbox", "FALSE")
    monkeypatch.setenv("TVE_EMAIL_INTAKE", "imap")  # exercise the legacy poller

    async def configuration():
        return config

    async def forbidden(*args):
        pytest.fail("followups ran with invalid checkpoint state")

    async def stop(*args):
        raise asyncio.CancelledError

    async def close():
        pass

    monkeypatch.setattr(imap_intake, "_imap_config", configuration)
    monkeypatch.setattr(imap_intake, "deliver_due_followups", forbidden)
    monkeypatch.setattr(imap_intake.asyncio, "sleep", stop)
    monkeypatch.setattr(database, "close_pool", close)
    with pytest.raises(asyncio.CancelledError):
        await imap_intake.run()
    assert "error_type=CheckpointError" in caplog.text
    assert "private-corrupt-data" not in caplog.text


@pytest.mark.asyncio
async def test_sandbox_worker_bypasses_checkpoint_and_real_imap(worker, monkeypatch):
    _, _, _, opened, directory = worker
    monkeypatch.delenv("SANDBOX", raising=False)
    monkeypatch.setenv("sandbox", "TRUE")
    monkeypatch.setenv("TVE_IMAP_STATE_DIR", str(directory / "unavailable"))
    calls = []

    async def poll(*args):
        calls.append("mailpit")

    async def followups(*args):
        calls.append("followups")

    async def stop(*args):
        raise asyncio.CancelledError

    async def close():
        pass

    async def sandbox_ready():
        pass

    monkeypatch.setattr(imap_intake, "poll_mailpit", poll)
    monkeypatch.setattr(imap_intake, "deliver_due_followups", followups)
    monkeypatch.setattr(imap_intake.asyncio, "sleep", stop)
    monkeypatch.setattr(database, "close_pool", close)
    monkeypatch.setattr(database, "verify_sandbox_storage", sandbox_ready)
    with pytest.raises(asyncio.CancelledError):
        await imap_intake.run()
    assert calls == ["mailpit", "followups"]
    assert not opened
    assert not (directory / "checkpoint.json").exists()
