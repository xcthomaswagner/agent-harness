#!/usr/bin/env python3
"""Parse a saved post-mortem-analyst Claude session for its three output sections.

The post-mortem-analyst skill (``runtime/skills/post-mortem-analyst/SKILL.md``)
contracts that its final message ends with exactly three markdown headers, in
this order::

    ## Root cause
    ## Proposed fix
    ## Memory entry

This helper runs after a ``claude -p`` discuss session. It reads the saved
transcript (either a plain text dump or the JSONL output of
``claude -p --output-format stream-json``), pulls out the three sections, and
prints a summary. With ``--apply-fix`` it also runs ``git apply --check``
against the proposed fix to see if it applies cleanly. With ``--save-memory``
it writes the memory entry to a temp file the developer can review.

No section is applied automatically — the script is read-only on the
repository. Application is the developer's decision after they have read
what was extracted.

Usage::

    python scripts/capture_discuss_output.py --transcript <path>
    python scripts/capture_discuss_output.py --transcript <path> --apply-fix
    python scripts/capture_discuss_output.py --transcript <path> --save-memory

Exit codes:
    0  all three sections found and extracted
    1  parse failure (missing or misordered sections)
    2  transcript file is unreadable
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

# The three required section headers, in the exact order the skill contracts.
# ``re.MULTILINE`` anchors ``^``/``$`` to line boundaries; the patterns are
# compared character-for-character (no case-fold, no whitespace tolerance).
REQUIRED_SECTIONS: tuple[str, ...] = (
    "## Root cause",
    "## Proposed fix",
    "## Memory entry",
)


def _read_transcript_text(path: Path) -> str:
    """Return the transcript content as a single string.

    Supports two formats produced by the discuss workflow:

    * Plain text (what the developer gets by copy-pasting the terminal).
    * JSONL ``stream-json`` output from ``claude -p --output-format
      stream-json``: one JSON object per line, with assistant messages
      containing ``content`` blocks of ``{"type": "text", "text": ...}``.

    Detection: try to parse the first non-empty line as JSON. If it looks
    like a stream-json event, walk the full file and concatenate every
    assistant text block. Otherwise, return the raw file contents.
    """
    raw = path.read_text(encoding="utf-8")

    first_nonempty = ""
    for line in raw.splitlines():
        if line.strip():
            first_nonempty = line.strip()
            break

    if not first_nonempty or not first_nonempty.startswith("{"):
        return raw

    try:
        first_obj = json.loads(first_nonempty)
    except json.JSONDecodeError:
        return raw

    if not isinstance(first_obj, dict) or "type" not in first_obj:
        return raw

    # Looks like stream-json. Walk every line and extract assistant text.
    chunks: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        # stream-json nests the assistant message under "message" -> "content"
        message = event.get("message")
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text")
                        if isinstance(text, str):
                            chunks.append(text)

        # Some SDK builds put the text directly on the event.
        if event.get("type") == "text":
            text = event.get("text")
            if isinstance(text, str):
                chunks.append(text)

    return "\n".join(chunks)


def parse_sections(text: str) -> dict[str, str]:
    """Extract the three required sections from ``text``.

    Returns a dict keyed by header (e.g. ``"## Root cause"``) with section
    bodies as values (the text between one header and the next, or EOF for
    the final section). Leading/trailing whitespace is stripped from each
    body, so a header with no body yields an empty string.

    Raises ``ValueError`` if any section is missing OR if sections appear
    in the wrong order. Extra content before the first header or after
    the last section is silently tolerated.
    """
    # Find each header's position in the text. We do one pass per header
    # rather than a single regex so the error message can name exactly
    # which headers are missing.
    positions: dict[str, int] = {}
    for header in REQUIRED_SECTIONS:
        # Use re.escape so the ``##`` and spaces match literally, and
        # anchor to start-of-line with MULTILINE.
        pattern = re.compile(rf"^{re.escape(header)}$", re.MULTILINE)
        match = pattern.search(text)
        if match is None:
            continue
        positions[header] = match.start()

    missing = [h for h in REQUIRED_SECTIONS if h not in positions]
    if missing:
        found = [h for h in REQUIRED_SECTIONS if h in positions]
        raise ValueError(
            "Missing required section(s): "
            + ", ".join(missing)
            + (f". Found: {', '.join(found)}" if found else ". Found: (none)")
        )

    # Verify order. The positions dict must be monotonically increasing
    # in REQUIRED_SECTIONS order.
    ordered_positions = [positions[h] for h in REQUIRED_SECTIONS]
    if ordered_positions != sorted(ordered_positions):
        actual_order = sorted(REQUIRED_SECTIONS, key=lambda h: positions[h])
        raise ValueError(
            "Sections are in the wrong order. Expected: "
            + " -> ".join(REQUIRED_SECTIONS)
            + ". Actual: "
            + " -> ".join(actual_order)
        )

    # Extract each section body: from the end of its header line to the
    # start of the next header (or EOF for the last section).
    bodies: dict[str, str] = {}
    for idx, header in enumerate(REQUIRED_SECTIONS):
        header_start = positions[header]
        # Body starts at the newline after the header line.
        body_start = text.find("\n", header_start)
        if body_start == -1:
            body_start = header_start + len(header)
        else:
            body_start += 1

        if idx + 1 < len(REQUIRED_SECTIONS):
            body_end = positions[REQUIRED_SECTIONS[idx + 1]]
        else:
            body_end = len(text)

        bodies[header] = text[body_start:body_end].strip()

    return bodies


def looks_like_unified_diff(fix_text: str) -> bool:
    """Return True if ``fix_text`` contains a unified diff signature.

    Heuristic: the classic ``diff --git`` prefix, or a ``--- `` / ``+++ ``
    header pair. We look for either in the first 20 non-empty lines so a
    diff inside a fenced code block is detected even if there's prose
    around it.
    """
    head_lines = [ln for ln in fix_text.splitlines() if ln.strip()][:40]
    for line in head_lines:
        stripped = line.lstrip()
        if stripped.startswith("diff --git "):
            return True
        if stripped.startswith("--- ") or stripped.startswith("--- a/"):
            return True
    return False


def _extract_diff_from_fenced_block(fix_text: str) -> str:
    """Return just the diff content if the fix is wrapped in a fenced block.

    The post-mortem-analyst example in the skill wraps the diff in a
    ``````diff`` fence. ``git apply`` wants the raw diff, not the markdown
    wrapper. If no fence is present, return ``fix_text`` unchanged.
    """
    lines = fix_text.splitlines()
    in_block = False
    collected: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not in_block and stripped.startswith("```"):
            in_block = True
            continue
        if in_block and stripped == "```":
            break
        if in_block:
            collected.append(line)
    if collected:
        return "\n".join(collected) + "\n"
    return fix_text


def check_git_apply(fix_text: str) -> tuple[bool, str]:
    """Run ``git apply --check`` against ``fix_text``.

    Returns ``(ok, message)``. ``ok`` is True if git apply would succeed.
    ``message`` is stderr on failure or a success marker on success. The
    diff is written to a temp file; we do NOT call ``git apply`` without
    ``--check`` — the developer applies fixes themselves.
    """
    diff_body = _extract_diff_from_fenced_block(fix_text)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".patch", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(diff_body)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            ["git", "apply", "--check", tmp_path],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return True, f"git apply --check OK (patch at {tmp_path})"
        return False, result.stderr.strip() or result.stdout.strip()
    finally:
        # Leave the temp file on disk — the caller may want to inspect
        # or re-run git apply manually. Print the path so it's visible.
        pass


def _one_line_preview(text: str, max_len: int = 100) -> str:
    """Return the first non-empty line of ``text``, truncated to ``max_len``."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            if len(stripped) > max_len:
                return stripped[: max_len - 3] + "..."
            return stripped
    return "(empty)"


def _save_memory_entry(body: str) -> Path:
    """Write the memory entry body to a temp file and return the path."""
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        prefix="post-mortem-memory-",
        delete=False,
        encoding="utf-8",
    ) as tmp:
        tmp.write(body)
        if not body.endswith("\n"):
            tmp.write("\n")
        return Path(tmp.name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Parse a saved post-mortem-analyst Claude session transcript "
            "for its three structured output sections."
        )
    )
    parser.add_argument(
        "--transcript",
        required=True,
        type=Path,
        help=(
            "Path to the saved session transcript. Accepts a plain text "
            "file or the JSONL stream from `claude -p --output-format stream-json`."
        ),
    )
    parser.add_argument(
        "--apply-fix",
        action="store_true",
        help=(
            "If the proposed fix looks like a unified diff, run "
            "`git apply --check` to verify it applies cleanly. Does NOT "
            "actually apply the patch."
        ),
    )
    parser.add_argument(
        "--save-memory",
        action="store_true",
        help="Write the memory entry body to a temp file and print its path.",
    )
    args = parser.parse_args(argv)

    transcript_path: Path = args.transcript
    try:
        text = _read_transcript_text(transcript_path)
    except OSError as exc:
        print(f"error: cannot read transcript {transcript_path}: {exc}", file=sys.stderr)
        return 2

    try:
        sections = parse_sections(text)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    root_cause = sections["## Root cause"]
    proposed_fix = sections["## Proposed fix"]
    memory_entry = sections["## Memory entry"]

    fix_lines = len([ln for ln in proposed_fix.splitlines() if ln.strip()])
    memory_lines = len([ln for ln in memory_entry.splitlines() if ln.strip()])

    print(f"Root cause: {_one_line_preview(root_cause)}")
    print(f"Proposed fix: {fix_lines} lines")
    print(f"Memory entry: {memory_lines} lines")

    if args.apply_fix:
        if looks_like_unified_diff(proposed_fix):
            ok, message = check_git_apply(proposed_fix)
            status = "applies cleanly" if ok else "DOES NOT apply"
            print(f"git apply --check: {status}")
            if message:
                print(f"  {message}")
        else:
            print("proposed fix is not a unified diff; skipping git-apply check.")

    if args.save_memory:
        memory_path = _save_memory_entry(memory_entry)
        print(f"memory entry saved to: {memory_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
