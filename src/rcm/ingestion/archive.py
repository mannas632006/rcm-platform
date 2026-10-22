"""Immutable RAW archive (M1.4; PD-9, PD-10, DR-01).

    PD-9  Raw inbound files archived immutably **before** parsing.
    PD-10 All layers rebuildable from RAW + code. No hidden state.

The ordering in PD-9 is the whole requirement, and it is easy to get backwards.
The tempting shape is parse, and if it worked, keep the file -- which loses
precisely the files worth keeping. A file that crashes the parser is the one
nobody can reproduce from a log, and the one a payer will not resend because as
far as they are concerned they already sent it.

PD-10 is the other half. Every later layer is a function of RAW plus code, so
RAW is the only thing that cannot be rebuilt. That makes it write-once: nothing
in this module updates or deletes an archived file, and `archive()` refuses to
overwrite. An archive that can be rewritten is a log that can be made to agree
with whatever the current code believes.

**Content addressing gives idempotency for free.** The storage path is derived
from the SHA-256 of the bytes, so the same file archived twice lands in the
same place and is recognised as already present. That is what makes
reprocessing the corpus a no-op (M1 gate) rather than a duplicate-detection
problem solved separately in every loader.

The hash is of the **bytes**, before any decoding. Two files that differ only
in line endings are different files here, and that is correct: they are
different bytes from the sender, and deciding they are equivalent is a parsing
judgement that RAW must not make.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from rcm.platform.errors import ComplianceError
from rcm.platform.logging import get_logger

log = get_logger(__name__)

#: Read in chunks so a large file is hashed without being held in memory twice.
_HASH_CHUNK_BYTES: Final = 1024 * 1024

#: Mode 0o444: read-only for everyone, including the owner. The operating
#: system is not the control -- a process running as the owner can chmod it
#: back -- but it turns an accidental overwrite into an error at the point it
#: happens rather than a silent change discovered during an audit.
_READ_ONLY: Final = 0o444


class TransactionKind(StrEnum):
    """What an inbound file is, as far as RAW is concerned.

    Deliberately coarse. RAW records what arrived; deciding that a file is a
    *valid* 835 is the parser's job and happens after this.
    """

    REMITTANCE_835 = "835"
    CLAIM_837 = "837"
    ACKNOWLEDGEMENT_999 = "999"
    CLAIM_ACKNOWLEDGEMENT_277CA = "277CA"
    ELIGIBILITY_271 = "271"
    STATUS_277 = "277"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ArchivedFile:
    """A file that is now in RAW and can be read back byte-for-byte."""

    #: SHA-256 of the bytes as received. The identity of the file.
    content_hash: str
    byte_size: int
    #: Path within the archive root, not an absolute path: the root moves
    #: between environments and a stored absolute path would not survive it.
    relative_path: str
    original_filename: str
    received_at: datetime
    transaction_kind: TransactionKind
    #: True when these exact bytes were already archived. The caller uses this
    #: to skip reprocessing rather than to decide whether the file is valid.
    already_present: bool

    @property
    def short_hash(self) -> str:
        """First 12 hex characters, for log lines and operator conversation."""
        return self.content_hash[:12]


def content_hash(raw: bytes) -> str:
    """SHA-256 of the bytes exactly as received."""
    digest = hashlib.sha256()
    for start in range(0, len(raw), _HASH_CHUNK_BYTES):
        digest.update(raw[start : start + _HASH_CHUNK_BYTES])
    return digest.hexdigest()


def detect_transaction_kind(raw: bytes) -> TransactionKind:
    """Classify a file by its ST01, without committing to parsing it.

    A best-effort scan, on purpose. The point is to label the archived file so
    an operator can find it later; a file this cannot classify is still
    archived, as UNKNOWN. Refusing to archive something because its type is
    unclear would lose exactly the files that most need keeping.
    """
    head = raw[:4096]
    for marker, kind in (
        (b"*835*", TransactionKind.REMITTANCE_835),
        (b"*837*", TransactionKind.CLAIM_837),
        (b"*277*", TransactionKind.CLAIM_ACKNOWLEDGEMENT_277CA),
        (b"*271*", TransactionKind.ELIGIBILITY_271),
        (b"*999*", TransactionKind.ACKNOWLEDGEMENT_999),
    ):
        if marker in head:
            return kind
    return TransactionKind.UNKNOWN


def _storage_path(digest: str, kind: TransactionKind, received: datetime) -> str:
    """Where a file lives inside the archive root.

    Partitioned by receipt date and then by the first two bytes of the hash.
    The date partition is what makes "everything we received on the 14th"
    answerable without a database; the hash prefix keeps any one directory from
    collecting millions of entries, which is a problem on most filesystems long
    before it is a problem for the code.
    """
    return f"{kind.value}/{received:%Y/%m/%d}/{digest[:2]}/{digest}.edi"


def archive(
    raw: bytes,
    *,
    original_filename: str,
    archive_root: Path | str,
    received_at: datetime | None = None,
    immutable: bool = True,
) -> ArchivedFile:
    """Write `raw` into the archive and return its record. Never overwrites.

    Called **before** the file is parsed. `tests/integration/test_raw_archive.py`
    asserts that ordering by failing the parse and checking the file is still
    there.

    The write is atomic: the bytes land in a temporary file in the same
    directory and are renamed into place. A partially written archive entry
    would be worse than none, because it looks like a complete record of
    something that never fully arrived.
    """
    received = received_at or datetime.now(UTC)
    digest = content_hash(raw)
    kind = detect_transaction_kind(raw)
    relative = _storage_path(digest, kind, received)

    root = Path(archive_root)
    destination = root / relative

    if destination.exists():
        existing_size = destination.stat().st_size
        if existing_size != len(raw):
            # Same SHA-256, different length. This is a hash collision or a
            # corrupted archive, and both are serious enough to stop rather
            # than resolve by guessing which copy is right.
            raise ComplianceError(
                reason="archived file has the same hash but a different size",
                content_hash=digest,
                archived_size=existing_size,
                incoming_size=len(raw),
            )
        log.info("raw.already_archived", content_hash=digest[:12], kind=str(kind))
        return ArchivedFile(
            content_hash=digest,
            byte_size=len(raw),
            relative_path=relative,
            original_filename=original_filename,
            received_at=received,
            transaction_kind=kind,
            already_present=True,
        )

    destination.parent.mkdir(parents=True, exist_ok=True)

    # Atomic: write beside the target, then rename. `Path.replace` is a rename
    # within one filesystem, which is atomic, so a reader never sees a
    # half-written archive entry.
    handle, temporary_name = tempfile.mkstemp(dir=destination.parent, suffix=".partial")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(raw)
            stream.flush()
            # fsync before rename: without it a crash can leave the directory
            # entry pointing at a file whose contents never reached the disk.
            os.fsync(stream.fileno())
        if immutable:
            temporary.chmod(_READ_ONLY)
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise

    log.info(
        "raw.archived",
        content_hash=digest[:12],
        kind=str(kind),
        bytes=len(raw),
    )
    return ArchivedFile(
        content_hash=digest,
        byte_size=len(raw),
        relative_path=relative,
        original_filename=original_filename,
        received_at=received,
        transaction_kind=kind,
        already_present=False,
    )


def read_archived(relative_path: str, *, archive_root: Path | str) -> bytes:
    """Read a file back out of RAW.

    PD-10's reconstruction requirement runs through here: every later layer is
    rebuildable from these bytes plus code.
    """
    path = Path(archive_root) / relative_path
    if not path.is_file():
        raise ComplianceError(
            reason="archived file is missing from RAW",
            relative_path=relative_path,
        )
    return path.read_bytes()


def verify_archived(record: ArchivedFile, *, archive_root: Path | str) -> bool:
    """Re-hash an archived file and compare it with what was recorded.

    Not called on the hot path. This is for the periodic integrity sweep and
    for an operator asking whether RAW still holds what it claims to.
    """
    stored = read_archived(record.relative_path, archive_root=archive_root)
    return content_hash(stored) == record.content_hash and len(stored) == record.byte_size
