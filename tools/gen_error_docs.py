"""Render `docs/errors.md` from the error taxonomy.

M0.6's acceptance is that *every error class maps to a documented response*. Hand-
written documentation drifts the moment someone adds a class in a hurry, so the
document is generated and `tests/unit/test_error_taxonomy.py` fails the build when
the checked-in copy no longer matches.

    python -m tools.gen_error_docs > docs/errors.md
"""

from __future__ import annotations

import inspect
import sys

from rcm.platform.errors import PROBLEM_TYPE_BASE, PlatformError, all_error_classes


def _summary(cls: type[PlatformError]) -> str:
    """First sentence of the class docstring, or the fixed detail if it has none."""
    doc = inspect.getdoc(cls)
    if not doc or cls.__doc__ is None:
        return cls.detail
    first = doc.split("\n\n", 1)[0].replace("\n", " ").strip()
    return first


def render() -> str:
    lines: list[str] = [
        "# Error taxonomy",
        "",
        "**Generated file — do not edit.** Regenerate with:",
        "",
        "```",
        "python -m tools.gen_error_docs > docs/errors.md",
        "```",
        "",
        "Every error the platform raises deliberately is listed here with the exact",
        "response it produces. Responses follow RFC 7807 Problem Details (FR-API-04);",
        f"the `type` URI is `{PROBLEM_TYPE_BASE}<code>`.",
        "",
        "Clients branch on `code`. Titles and details are human-readable and may change.",
        "",
        "`detail` is fixed per class and never interpolates data — variable values go in",
        "`context`, which passes through the PHI scrubber before serialisation (PD-13).",
        "",
        "**Suspends automation** marks the compliance-class errors that halt the affected",
        "automation path pending human review (FR-BE-07).",
        "",
        "| Code | HTTP | Title | Suspends automation | Meaning |",
        "|---|---|---|---|---|",
    ]

    for cls in sorted(all_error_classes(), key=lambda c: (c.status, c.code)):
        suspends = "yes" if cls.suspends_automation else "—"
        lines.append(
            f"| `{cls.code}` | {int(cls.status)} | {cls.title} | {suspends} | {_summary(cls)} |"
        )

    lines += [
        "",
        "## Response shape",
        "",
        "```json",
        "{",
        f'  "type": "{PROBLEM_TYPE_BASE}file-does-not-balance",',
        '  "title": "Remittance file does not balance",',
        '  "status": 422,',
        '  "detail": "The file failed a data quality gate and was quarantined unparsed.",',
        '  "code": "file-does-not-balance",',
        '  "instance": "/api/v1/remittances/ingest",',
        '  "request_id": "01JB8Z0Q2K7X",',
        '  "context": {"assertion": "clp_sum != bpr_total", "file_hash": "sha256:..."}',
        "}",
        "```",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    # Windows defaults stdout to the ANSI code page, which mangles the em-dashes
    # in this document when the caller redirects to a file.
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[union-attr]
    sys.stdout.write(render())
