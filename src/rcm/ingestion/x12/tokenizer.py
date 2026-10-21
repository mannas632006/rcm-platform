"""X12 lexical layer: separators, segments, and the limits that make it safe.

This module turns bytes that arrived from outside into a list of segments. It
knows nothing about claims, remittances or the domain -- `.importlinter`
enforces that, and the reason is SR-12: X12 is untrusted input. A parser that
could reach the domain directly would give a malformed file a path straight
into CORE.

Three properties this layer is responsible for.

**It never trusts the delimiters.** X12 does not fix them. The interchange
declares its own in the ISA segment, which is the one segment with fixed-width
fields precisely so that a reader can find them before it can parse anything.
Hard-coding `*` and `~` works until the first payer who uses `|`.

**It never crashes.** M1.1's acceptance is that malformed input never raises an
uncaught exception, so every failure here is a named `QuarantineError`
subclass. A `ValueError` escaping this module is a defect, not an edge case:
the caller's contract is that a bad file is quarantined, and an unnamed
exception cannot be quarantined because nothing knows what happened.

**It bounds everything before allocating.** The size and segment limits are
checked against the input, not against the result of parsing it -- a 2 GB file
must be rejected before it is split, not after.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from rcm.platform.errors import MalformedX12Error, X12LimitExceededError

#: The ISA segment is fixed-width by design: 105 characters plus the segment
#: terminator. Every delimiter an interchange uses can be read from it by
#: offset, which is the only way to start parsing a file whose delimiters are
#: not yet known.
ISA_SEGMENT_LENGTH: Final = 106
ISA_ELEMENT_SEPARATOR_INDEX: Final = 3
ISA_REPETITION_SEPARATOR_INDEX: Final = 82
ISA_COMPONENT_SEPARATOR_INDEX: Final = 104
ISA_SEGMENT_TERMINATOR_INDEX: Final = 105

#: Characters that may appear between segments without being part of one.
#: Line breaks after each segment are optional in X12 and common in practice;
#: a parser that requires them is as wrong as one that forbids them.
_SEGMENT_PADDING: Final = "\r\n\t "

#: A delimiter must be a single character, and must not be one that would make
#: the file unparseable.
_FORBIDDEN_DELIMITERS: Final = frozenset({"", " ", "\r", "\n"})


@dataclass(frozen=True, slots=True)
class Separators:
    """The four delimiters an interchange declares in its ISA segment."""

    element: str
    component: str
    repetition: str
    segment: str

    def validate(self) -> None:
        """Reject delimiter sets that cannot describe a parseable file."""
        found = {
            "element": self.element,
            "component": self.component,
            "repetition": self.repetition,
            "segment": self.segment,
        }
        for name, value in found.items():
            if value in _FORBIDDEN_DELIMITERS or len(value) != 1:
                raise MalformedX12Error(delimiter=name, reason="not a usable single character")

        # Two delimiters that are the same character make the file ambiguous:
        # there is no way to tell where an element ends and a segment begins.
        # The repetition separator is excluded from this check because `^` is
        # frequently sent as a literal when the sender uses no repetition, and
        # a collision there is harmless as long as it differs from the element
        # and segment separators.
        significant = [self.element, self.component, self.segment]
        if len(set(significant)) != len(significant):
            raise MalformedX12Error(reason="element, component and segment separators collide")
        if self.repetition in (self.element, self.segment):
            raise MalformedX12Error(reason="repetition separator collides with a structural one")


@dataclass(frozen=True, slots=True)
class Limits:
    """Bounds applied to untrusted input (SR-12, settings `x12_max_*`).

    Defaults mirror `rcm.platform.config.Settings`. They are parameters rather
    than imports so that this module stays free of application configuration
    and can be exercised with small limits in tests.
    """

    max_bytes: int = 200 * 1024 * 1024
    max_segments: int = 5_000_000
    #: A single segment with an absurd number of elements is the other way to
    #: make a small file expensive. Real segments are tens of elements; this is
    #: three orders of magnitude of headroom.
    max_elements_per_segment: int = 10_000


@dataclass(frozen=True, slots=True)
class Segment:
    """One X12 segment: its tag, its elements, and where it was found.

    `elements` excludes the tag, and `get` is **one-based** to match how X12
    names things. `CLP01` is `segment.get(1)`. Indexing a list zero-based while
    reading a specification that counts from one is a reliable source of
    off-by-one defects in exactly the code where they are most expensive.
    """

    tag: str
    elements: tuple[str, ...]
    #: 1-based position in the interchange, for error messages and quarantine
    #: reports. An operator told "segment 41,208 is malformed" can find it.
    position: int

    def get(self, index: int) -> str:
        """Element `index`, one-based. Absent or trailing-omitted elements are ''.

        X12 permits trailing empty elements to be dropped, so a missing element
        and an empty one are the same thing to a reader and are reported the
        same way here.
        """
        if index < 1:
            raise ValueError("X12 element indexes are one-based")
        position = index - 1
        if position >= len(self.elements):
            return ""
        return self.elements[position]

    def composite(self, index: int, separators: Separators) -> tuple[str, ...]:
        """Element `index` split into its components.

        An element with no component separator is a one-component composite,
        which keeps callers from needing two code paths.
        """
        value = self.get(index)
        if not value:
            return ()
        return tuple(value.split(separators.component))

    def __str__(self) -> str:
        return f"{self.tag}[{self.position}]"


def decode(raw: bytes) -> str:
    """Decode an inbound file to text without ever failing.

    The X12 basic character set is ASCII, but files arrive from systems that
    are not careful about it, and a payer's remittance is not something this
    platform gets to reject because of one accented character in a patient
    name. UTF-8 is tried first because it is what a modern sender produces;
    latin-1 is the fallback because it maps every byte to a character and so
    cannot fail. The content is preserved either way -- the alternative,
    decoding with `errors="replace"`, silently corrupts the data and is far
    worse than guessing an encoding.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def detect_separators(text: str) -> Separators:
    """Read the interchange's delimiters out of its ISA segment.

    Done by offset, not by searching, because the delimiters are what a search
    would need to know. This is the bootstrap, and it is why ISA is the one
    fixed-width segment in the standard.
    """
    if len(text) < ISA_SEGMENT_LENGTH:
        raise MalformedX12Error(reason="file is shorter than an ISA segment", length=len(text))
    if not text.startswith("ISA"):
        raise MalformedX12Error(reason="file does not begin with ISA", found=text[:3])

    separators = Separators(
        element=text[ISA_ELEMENT_SEPARATOR_INDEX],
        repetition=text[ISA_REPETITION_SEPARATOR_INDEX],
        component=text[ISA_COMPONENT_SEPARATOR_INDEX],
        segment=text[ISA_SEGMENT_TERMINATOR_INDEX],
    )
    separators.validate()
    return separators


def tokenize(
    source: str | bytes,
    *,
    limits: Limits | None = None,
) -> tuple[tuple[Segment, ...], Separators]:
    """Split an interchange into segments.

    Returns the segments and the separators that were used, because every later
    stage needs the latter to read composite elements and none of them should
    re-derive it.

    Limits are applied before the split, not after: rejecting a file for being
    too large only helps if the rejection happens before it is expanded into
    several million objects.
    """
    limits = limits or Limits()

    raw = source if isinstance(source, bytes) else source.encode("utf-8", errors="surrogateescape")
    if len(raw) > limits.max_bytes:
        raise X12LimitExceededError(limit="max_bytes", configured=limits.max_bytes, actual=len(raw))

    text = decode(raw) if isinstance(source, bytes) else source
    separators = detect_separators(text)

    segments: list[Segment] = []
    position = 0
    for chunk in text.split(separators.segment):
        stripped = chunk.strip(_SEGMENT_PADDING)
        if not stripped:
            # Whitespace between segments, and the empty tail after the final
            # terminator. Neither is a segment.
            continue

        position += 1
        if position > limits.max_segments:
            raise X12LimitExceededError(limit="max_segments", configured=limits.max_segments)

        parts = stripped.split(separators.element)
        if len(parts) > limits.max_elements_per_segment:
            raise X12LimitExceededError(
                limit="max_elements_per_segment",
                configured=limits.max_elements_per_segment,
                segment_position=position,
            )

        tag = parts[0].strip()
        if not tag:
            raise MalformedX12Error(reason="segment has no tag", segment_position=position)

        segments.append(Segment(tag=tag, elements=tuple(parts[1:]), position=position))

    if not segments:
        raise MalformedX12Error(reason="file contains no segments")
    return tuple(segments), separators
