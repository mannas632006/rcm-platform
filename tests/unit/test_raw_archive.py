"""RAW archive: write-once, content-addressed, byte-exact (M1.4; PD-9, PD-10).

The archive is the only layer that cannot be rebuilt. Everything else is a
function of these bytes plus code (PD-10), which is what makes "drop the marts
and rebuild" a safe sentence and what makes the file that crashed the parser
recoverable rather than lost.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from rcm.ingestion.archive import (
    TransactionKind,
    archive,
    content_hash,
    detect_transaction_kind,
    read_archived,
    verify_archived,
)
from rcm.platform.errors import ComplianceError

REMITTANCE = (
    b"ISA*00*          *00*          *ZZ*SENDER         *ZZ*RECEIVER       "
    b"*240131*0000*^*00501*000000001*0*T*:~"
    b"GS*HP*SENDER*RECEIVER*20240131*0000*1*X*005010X221A1~"
    b"ST*835*0001~BPR*I*0.00*C*ACH*CCP*01*9*DA*1*****20240131~SE*3*0001~"
    b"GE*1*1~IEA*1*000000001~"
)


def test_a_file_round_trips_byte_for_byte(tmp_path: Path) -> None:
    """The point of RAW. Anything less and PD-10's reconstruction is a guess."""
    record = archive(REMITTANCE, original_filename="r.edi", archive_root=tmp_path)

    assert read_archived(record.relative_path, archive_root=tmp_path) == REMITTANCE
    assert record.byte_size == len(REMITTANCE)
    assert verify_archived(record, archive_root=tmp_path)


def test_the_same_bytes_archive_once(tmp_path: Path) -> None:
    """Content addressing, which is what makes reprocessing a no-op."""
    first = archive(REMITTANCE, original_filename="a.edi", archive_root=tmp_path)
    second = archive(REMITTANCE, original_filename="b-renamed.edi", archive_root=tmp_path)

    assert first.already_present is False
    assert second.already_present is True
    assert first.relative_path == second.relative_path
    assert first.content_hash == second.content_hash

    stored = list(tmp_path.rglob("*.edi"))
    assert len(stored) == 1, "the same bytes were stored twice"


def test_different_bytes_are_different_files(tmp_path: Path) -> None:
    """Including when they differ only in line endings.

    Deciding that CRLF and LF variants are the same file is a parsing
    judgement, and RAW must not make parsing judgements -- they are different
    bytes from the sender.
    """
    crlf = REMITTANCE.replace(b"~", b"~\r\n")
    first = archive(REMITTANCE, original_filename="a.edi", archive_root=tmp_path)
    second = archive(crlf, original_filename="b.edi", archive_root=tmp_path)

    assert first.content_hash != second.content_hash
    assert len(list(tmp_path.rglob("*.edi"))) == 2


def test_an_archived_file_is_read_only(tmp_path: Path) -> None:
    """Not the control -- the owner can chmod it back -- but it turns an
    accidental overwrite into an error where it happens."""
    record = archive(REMITTANCE, original_filename="r.edi", archive_root=tmp_path)
    stored = tmp_path / record.relative_path

    assert stored.stat().st_mode & 0o222 == 0, "archived file is writable"


def test_nothing_partial_is_left_behind(tmp_path: Path) -> None:
    """The write is atomic: temporary file, then rename.

    A half-written archive entry is worse than none, because it looks like a
    complete record of something that never fully arrived.
    """
    archive(REMITTANCE, original_filename="r.edi", archive_root=tmp_path)

    assert not list(tmp_path.rglob("*.partial"))


def test_a_hash_collision_with_a_different_size_stops_rather_than_guesses(
    tmp_path: Path,
) -> None:
    """Same digest, different length. Corruption or collision -- both serious.

    Resolving it by picking a copy would be choosing which version of a
    financial record to keep, on no evidence.
    """
    record = archive(REMITTANCE, original_filename="r.edi", archive_root=tmp_path)
    stored = tmp_path / record.relative_path
    stored.chmod(0o644)
    stored.write_bytes(REMITTANCE + b"EXTRA")

    with pytest.raises(ComplianceError) as caught:
        archive(REMITTANCE, original_filename="r.edi", archive_root=tmp_path)

    assert "different size" in caught.value.context["reason"]


def test_verification_detects_a_tampered_archive(tmp_path: Path) -> None:
    record = archive(REMITTANCE, original_filename="r.edi", archive_root=tmp_path)
    stored = tmp_path / record.relative_path
    stored.chmod(0o644)
    stored.write_bytes(REMITTANCE.replace(b"0.00", b"9.99"))

    assert verify_archived(record, archive_root=tmp_path) is False


def test_a_missing_archived_file_is_a_named_failure(tmp_path: Path) -> None:
    with pytest.raises(ComplianceError):
        read_archived("835/2024/01/31/ab/missing.edi", archive_root=tmp_path)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (REMITTANCE, TransactionKind.REMITTANCE_835),
        (REMITTANCE.replace(b"*835*", b"*837*"), TransactionKind.CLAIM_837),
        (REMITTANCE.replace(b"*835*", b"*999*"), TransactionKind.ACKNOWLEDGEMENT_999),
        (b"not edi at all", TransactionKind.UNKNOWN),
        (b"", TransactionKind.UNKNOWN),
    ],
)
def test_transaction_kind_is_detected_without_parsing(
    payload: bytes, expected: TransactionKind
) -> None:
    assert detect_transaction_kind(payload) == expected


def test_an_unclassifiable_file_is_still_archived(tmp_path: Path) -> None:
    """Refusing to archive what cannot be classified loses exactly the files
    most worth keeping."""
    record = archive(b"\x00\x01garbage", original_filename="?.edi", archive_root=tmp_path)

    assert record.transaction_kind is TransactionKind.UNKNOWN
    assert read_archived(record.relative_path, archive_root=tmp_path) == b"\x00\x01garbage"


def test_storage_is_partitioned_by_date_and_hash(tmp_path: Path) -> None:
    """Answering "what arrived on the 14th" should not need a database, and no
    single directory should collect millions of entries."""
    received = datetime(2024, 3, 14, 9, 30, tzinfo=UTC)
    record = archive(
        REMITTANCE, original_filename="r.edi", archive_root=tmp_path, received_at=received
    )

    assert record.relative_path.startswith("835/2024/03/14/")
    assert record.content_hash[:2] in record.relative_path


def test_the_hash_is_of_the_bytes_not_of_a_decoded_string() -> None:
    """Decoding first would make two different files look identical whenever
    one of them is not valid UTF-8."""
    latin = "Müller".encode("latin-1")
    utf8 = "Müller".encode()

    assert latin != utf8
    assert content_hash(latin) != content_hash(utf8)


def test_hashing_is_chunked_and_handles_a_large_file(tmp_path: Path) -> None:
    large = REMITTANCE * 20_000  # a few megabytes
    record = archive(large, original_filename="big.edi", archive_root=tmp_path)

    assert record.byte_size == len(large)
    assert verify_archived(record, archive_root=tmp_path)
