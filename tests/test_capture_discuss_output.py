#!/usr/bin/env python3
"""Tests for scripts/capture_discuss_output.py.

The script parses a saved post-mortem-analyst session transcript for its
three required headers (``## Root cause``, ``## Proposed fix``,
``## Memory entry``) in that exact order. These tests cover the parser's
happy path, error paths, tolerance of surrounding prose, stream-json
format handling, unified-diff detection, and the CLI entry point.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load_capture_module():
    """Import ``scripts/capture_discuss_output.py`` as a module.

    Same approach ``test_spawn_team_watcher.py`` uses for ``spawn_team.py``
    — the scripts dir is not a Python package, so we load by path.
    """
    module_name = "_capture_discuss_output_under_test"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(
        module_name, SCRIPTS_DIR / "capture_discuss_output.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


# -------------------------------------------------------------------------
# Fixtures — inline transcripts.
# -------------------------------------------------------------------------


HAPPY_PATH_TEXT = """\
I investigated the failed run. Here are my findings.

## Root cause

The planner skipped Phase 2 because session-stream.jsonl line 1842 shows
the ticket-analyst emitted `platform: sitecore` for a Salesforce repo.

## Proposed fix

Edit `runtime/skills/ticket-analyst/SKILL.md`:

```diff
- if repo has "package.json" -> platform: sitecore
+ if repo has "sfdx-project.json" -> platform: salesforce
```

## Memory entry

# Platform detection must check sfdx-project.json before package.json

When a Salesforce repo also has a `package.json` (common for LWC
tooling), the ticket-analyst skill was classifying it as Sitecore.
"""


MISSING_ROOT_CAUSE_TEXT = """\
## Proposed fix

Do the thing.

## Memory entry

Remember the thing.
"""


WRONG_ORDER_TEXT = """\
## Proposed fix

Do the thing.

## Root cause

The thing is broken.

## Memory entry

Remember.
"""


PREAMBLE_EPILOGUE_TEXT = """\
Hey developer, let me walk you through what I found.
I spent a while in session-stream.jsonl. Here are my findings:


## Root cause

Line 42 of pipeline.jsonl shows the skip.

## Proposed fix

Edit line 17 of the skill.

## Memory entry

Platform detection bug.

---

Hope this helps! Let me know if you want me to dig further.
"""


EMPTY_SECTION_BODY_TEXT = """\
## Root cause
## Proposed fix

Something.

## Memory entry

Something else.
"""


DIFF_FIX_TEXT = """\
## Root cause

Skipped phase.

## Proposed fix

```diff
diff --git a/foo.py b/foo.py
--- a/foo.py
+++ b/foo.py
@@ -1 +1 @@
-old
+new
```

## Memory entry

Bug notes.
"""


NON_DIFF_FIX_TEXT = """\
## Root cause

Skipped phase.

## Proposed fix

Edit the skill file and change "sitecore" to "salesforce" in the
platform detection block.

## Memory entry

Bug notes.
"""


def _stream_json_transcript(text: str) -> str:
    """Wrap ``text`` in a fake Claude Code stream-json envelope.

    The real format is one JSON object per line; assistant messages have
    a ``message.content`` list with ``text`` blocks. We split the text
    into a couple of chunks so the parser exercises its concat path.
    """
    # Split roughly at the first "## Proposed fix" so we get multiple
    # assistant events rather than one monolithic block.
    split_idx = text.find("## Proposed fix")
    if split_idx == -1:
        chunks = [text]
    else:
        chunks = [text[:split_idx], text[split_idx:]]

    lines = [
        json.dumps(
            {"type": "system", "subtype": "init", "session_id": "abc"}
        )
    ]
    for chunk in chunks:
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": chunk}],
                    },
                }
            )
        )
    lines.append(json.dumps({"type": "result", "subtype": "success"}))
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------------------
# Parser tests.
# -------------------------------------------------------------------------


class TestParseSections:
    def test_happy_path_extracts_all_three(self) -> None:
        mod = _load_capture_module()
        sections = mod.parse_sections(HAPPY_PATH_TEXT)
        assert "The planner skipped Phase 2" in sections["## Root cause"]
        assert "ticket-analyst/SKILL.md" in sections["## Proposed fix"]
        assert "Platform detection must check" in sections["## Memory entry"]

    def test_missing_root_cause_raises(self) -> None:
        mod = _load_capture_module()
        with pytest.raises(ValueError) as excinfo:
            mod.parse_sections(MISSING_ROOT_CAUSE_TEXT)
        assert "Missing required section" in str(excinfo.value)
        assert "## Root cause" in str(excinfo.value)

    def test_wrong_order_raises(self) -> None:
        mod = _load_capture_module()
        with pytest.raises(ValueError) as excinfo:
            mod.parse_sections(WRONG_ORDER_TEXT)
        assert "wrong order" in str(excinfo.value).lower()

    def test_preamble_and_epilogue_tolerated(self) -> None:
        mod = _load_capture_module()
        sections = mod.parse_sections(PREAMBLE_EPILOGUE_TEXT)
        assert "Line 42 of pipeline.jsonl" in sections["## Root cause"]
        assert "Edit line 17" in sections["## Proposed fix"]
        # Epilogue text after the last section is silently tolerated.
        # It ends up inside the memory entry body, which is fine — the
        # contract is that the memory entry runs to EOF.
        assert "Platform detection bug" in sections["## Memory entry"]

    def test_empty_section_body(self) -> None:
        mod = _load_capture_module()
        sections = mod.parse_sections(EMPTY_SECTION_BODY_TEXT)
        assert sections["## Root cause"] == ""
        assert "Something." in sections["## Proposed fix"]
        assert "Something else." in sections["## Memory entry"]

    def test_unified_diff_detection_positive(self) -> None:
        mod = _load_capture_module()
        sections = mod.parse_sections(DIFF_FIX_TEXT)
        assert mod.looks_like_unified_diff(sections["## Proposed fix"]) is True

    def test_unified_diff_detection_negative(self) -> None:
        mod = _load_capture_module()
        sections = mod.parse_sections(NON_DIFF_FIX_TEXT)
        assert mod.looks_like_unified_diff(sections["## Proposed fix"]) is False

    def test_stream_json_transcript(self, tmp_path: Path) -> None:
        """Parser walks assistant text blocks across multiple stream-json events."""
        mod = _load_capture_module()
        transcript_path = tmp_path / "stream.jsonl"
        transcript_path.write_text(_stream_json_transcript(HAPPY_PATH_TEXT))

        text = mod._read_transcript_text(transcript_path)
        sections = mod.parse_sections(text)
        assert "The planner skipped Phase 2" in sections["## Root cause"]
        assert "ticket-analyst/SKILL.md" in sections["## Proposed fix"]
        assert "Platform detection must check" in sections["## Memory entry"]


# -------------------------------------------------------------------------
# CLI tests.
# -------------------------------------------------------------------------


class TestCli:
    def test_happy_path_exits_zero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mod = _load_capture_module()
        transcript_path = tmp_path / "transcript.md"
        transcript_path.write_text(HAPPY_PATH_TEXT)

        rc = mod.main(["--transcript", str(transcript_path)])
        captured = capsys.readouterr()
        assert rc == 0
        assert "Root cause:" in captured.out
        assert "Proposed fix:" in captured.out
        assert "Memory entry:" in captured.out

    def test_apply_fix_on_non_diff_prints_skip_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mod = _load_capture_module()
        transcript_path = tmp_path / "transcript.md"
        transcript_path.write_text(NON_DIFF_FIX_TEXT)

        rc = mod.main(["--transcript", str(transcript_path), "--apply-fix"])
        captured = capsys.readouterr()
        assert rc == 0
        assert "not a unified diff" in captured.out
        assert "skipping git-apply check" in captured.out

    def test_unreadable_transcript_exits_two(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        mod = _load_capture_module()
        missing = tmp_path / "does-not-exist.md"
        rc = mod.main(["--transcript", str(missing)])
        captured = capsys.readouterr()
        assert rc == 2
        assert "cannot read transcript" in captured.err
