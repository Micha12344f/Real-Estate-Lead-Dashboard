"""Private, bounded IMAP replay state, independent of application databases.

One worker owns the lock for a complete poll. Atomic replacement plus file and
directory fsync make completed checkpoints durable on the Linux Docker volume.
Any storage/validation failure stops intake; it never becomes a UID-zero cursor.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

MAX_IDENTITIES = 128
MAX_DEDUPE_KEYS = 8192
MAX_STATE_BYTES = 1024 * 1024
MAX_UID = 2**32 - 1
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class CheckpointError(RuntimeError):
    """A sanitized fail-closed error, containing no mailbox/provider values."""


def identity_hash(config: dict) -> str:
    mailbox = config["mailbox"]
    identity = [config["host"].lower(), config["port"], config["username"],
                "INBOX" if mailbox.upper() == "INBOX" else mailbox]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def dedupe_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _uid(value, *, minimum=0):
    return type(value) is int and minimum <= value <= MAX_UID


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CheckpointError("imap_checkpoint_invalid")
        result[key] = value
    return result


def _validate(state):
    if (not isinstance(state, dict) or set(state) != {"version", "mailboxes", "dedupe"}
            or type(state["version"]) is not int or state["version"] != 1
            or not isinstance(state["mailboxes"], dict)
            or len(state["mailboxes"]) > MAX_IDENTITIES
            or not isinstance(state["dedupe"], list)
            or len(state["dedupe"]) > MAX_DEDUPE_KEYS):
        raise CheckpointError("imap_checkpoint_invalid")
    for identity, cursor in state["mailboxes"].items():
        if (not _HASH.fullmatch(identity) or not isinstance(cursor, dict)
                or set(cursor) != {"uid_validity", "last_uid"}
                or not _uid(cursor["uid_validity"], minimum=1)
                or not _uid(cursor["last_uid"])):
            raise CheckpointError("imap_checkpoint_invalid")
    if (any(not isinstance(key, str) or not _HASH.fullmatch(key) for key in state["dedupe"])
            or len(set(state["dedupe"])) != len(state["dedupe"])):
        raise CheckpointError("imap_checkpoint_invalid")


class CheckpointStore:
    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(directory or os.environ.get("TVE_IMAP_STATE_DIR", "/var/lib/tve-imap"))
        self.path = self.directory / "checkpoint.json"
        self._lock = None
        self.state = None

    def __enter__(self):
        try:
            # Provisioning must supply the directory on persistent storage.
            # Do not silently substitute /tmp or create a missing mount path.
            if not self.directory.is_dir() or self.directory.is_symlink():
                raise CheckpointError("imap_checkpoint_directory_unavailable")
            lock_path = self.directory / "checkpoint.lock"
            if lock_path.is_symlink() or self.path.is_symlink():
                raise CheckpointError("imap_checkpoint_invalid")
            fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            self._lock = os.fdopen(fd, "r+b")
            if os.name == "nt":
                import msvcrt
                if os.fstat(fd).st_size == 0:
                    self._lock.write(b"\0")
                    self._lock.flush()
                self._lock.seek(0)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                with self.path.open("rb") as stream:
                    raw = stream.read(MAX_STATE_BYTES + 1)
                if len(raw) > MAX_STATE_BYTES:
                    raise CheckpointError("imap_checkpoint_invalid")
                self.state = json.loads(raw, object_pairs_hook=_unique_object)
            except FileNotFoundError:
                self.state = {"version": 1, "mailboxes": {}, "dedupe": []}
            _validate(self.state)
            # Prove atomic writes work before any provider fetch/intake.
            self.save()
            return self
        except Exception as exc:
            self.__exit__(None, None, None)
            if isinstance(exc, CheckpointError):
                raise
            raise CheckpointError("imap_checkpoint_unavailable") from None

    def __exit__(self, *args):
        if self._lock is not None:
            self._lock.close()  # releases the OS lock, including after crashes
            self._lock = None

    def save(self):
        temporary = None
        try:
            _validate(self.state)
            raw = json.dumps(self.state, separators=(",", ":"), sort_keys=True).encode()
            if len(raw) > MAX_STATE_BYTES:
                raise CheckpointError("imap_checkpoint_capacity")
            # Fixed temporary name under the poll lock bounds disk usage even
            # after repeated power loss; never accumulate orphan temp files.
            temporary = self.directory / ".checkpoint.tmp"
            if temporary.is_symlink():
                raise CheckpointError("imap_checkpoint_invalid")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as stream:
                if os.name != "nt":
                    os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
            if os.name != "nt":
                fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except Exception as exc:
            if isinstance(exc, CheckpointError):
                raise
            raise CheckpointError("imap_checkpoint_write_failed") from None
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass

    def prepare(self, identity: str, uid_validity: int, uid_next: int) -> tuple[int, str | None]:
        if not _uid(uid_validity, minimum=1) or not _uid(uid_next, minimum=1):
            raise CheckpointError("imap_checkpoint_metadata_invalid")
        cursor = self.state["mailboxes"].get(identity)
        reason = None
        if cursor is None:
            if len(self.state["mailboxes"]) >= MAX_IDENTITIES:
                # Preserve old identities instead of evicting their checkpoints.
                raise CheckpointError("imap_checkpoint_identity_capacity")
            reason = "first_connection"
        elif cursor["uid_validity"] != uid_validity:
            reason = "uidvalidity_changed"
        elif cursor["last_uid"] >= uid_next:
            raise CheckpointError("imap_checkpoint_uid_regression")
        if reason:
            self.state["mailboxes"][identity] = {"uid_validity": uid_validity, "last_uid": uid_next - 1}
            self.save()
        return self.state["mailboxes"][identity]["last_uid"], reason

    def contains(self, key: str) -> bool:
        return dedupe_hash(key) in self.state["dedupe"]

    def complete(self, identity: str, uid: int, key: str):
        cursor = self.state["mailboxes"][identity]
        if not _uid(uid, minimum=1) or uid <= cursor["last_uid"]:
            raise CheckpointError("imap_checkpoint_uid_invalid")
        digest = dedupe_hash(key)
        recent = self.state["dedupe"]
        if digest in recent:
            recent.remove(digest)
        recent.append(digest)
        del recent[:-MAX_DEDUPE_KEYS]
        cursor["last_uid"] = uid
        self.save()
