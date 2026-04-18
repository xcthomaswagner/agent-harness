"""SQLite-backed store for autonomy metrics.

This subpackage replaces the former monolithic ``autonomy_store.py``.
The file was split into domain-focused modules for readability but
every public symbol is re-exported from here — so existing callers
(``from autonomy_store import X``) continue to work unchanged.

Module layout:

* ``schema``     — connection + schema + migrations + FastAPI dependency
* ``pr_runs``    — ``pr_runs`` table CRUD
* ``issues``     — ``review_issues`` + ``pending_ai_issues`` + matches + commits
* ``defects``    — ``defect_links`` + ``manual_overrides`` + match promotion
* ``auto_merge`` — auto-merge decisions + kill-switch toggle
* ``lessons``    — self-learning lesson tables + lesson outcomes (v5)

All cross-module references (e.g. ``_now_iso``, ``AI_SOURCES``,
``insert_manual_override``) are resolved by explicit relative
imports — there are no circular imports.

Every public and private symbol the old file exposed is re-exported
here. ``from X import Y as Y`` is used as the mypy-sanctioned
re-export form so callers doing ``from autonomy_store import _now_iso``
or ``import autonomy_store as store; store._LESSON_STATUS_TRANSITIONS``
work without edits.
"""

from __future__ import annotations

from .auto_merge import _escape_like as _escape_like
from .auto_merge import get_auto_merge_toggle as get_auto_merge_toggle
from .auto_merge import list_recent_auto_merge_decisions as list_recent_auto_merge_decisions
from .auto_merge import record_auto_merge_decision as record_auto_merge_decision
from .auto_merge import set_auto_merge_toggle as set_auto_merge_toggle
from .defects import _parse_iso as _parse_iso
from .defects import count_merged_pr_runs_with_escape as count_merged_pr_runs_with_escape
from .defects import create_manual_match as create_manual_match
from .defects import get_defect_link as get_defect_link
from .defects import get_latest_defect_sweep_heartbeat as get_latest_defect_sweep_heartbeat
from .defects import insert_defect_link as insert_defect_link
from .defects import insert_manual_override as insert_manual_override
from .defects import list_confirmed_escaped_defects as list_confirmed_escaped_defects
from .defects import list_defect_links_for_profile as list_defect_links_for_profile
from .defects import promote_match_to_counted as promote_match_to_counted
from .defects import record_defect_sweep_heartbeat as record_defect_sweep_heartbeat
from .issues import drain_pending_ai_issues as drain_pending_ai_issues
from .issues import insert_issue_match as insert_issue_match
from .issues import insert_pending_ai_issue as insert_pending_ai_issue
from .issues import insert_pr_commit as insert_pr_commit
from .issues import insert_review_issue as insert_review_issue
from .issues import list_human_issues_for_pr_run as list_human_issues_for_pr_run
from .issues import list_issue_matches_for_human as list_issue_matches_for_human
from .issues import list_pr_commits as list_pr_commits
from .issues import list_review_issues_by_pr_run as list_review_issues_by_pr_run
from .issues import set_human_issue_code_change_flag as set_human_issue_code_change_flag
from .lessons import _APPLIED_LESSONS_LIMIT as _APPLIED_LESSONS_LIMIT
from .lessons import _LESSON_STATUS_TRANSITIONS as _LESSON_STATUS_TRANSITIONS
from .lessons import _TERMINAL_VERDICTS as _TERMINAL_VERDICTS
from .lessons import LESSON_EVIDENCE_CAP as LESSON_EVIDENCE_CAP
from .lessons import LESSON_REASON_MAX_LEN as LESSON_REASON_MAX_LEN
from .lessons import LESSON_SNIPPET_MAX_LEN as LESSON_SNIPPET_MAX_LEN
from .lessons import LessonCandidateUpsert as LessonCandidateUpsert
from .lessons import LessonOutcomeInsert as LessonOutcomeInsert
from .lessons import get_latest_outcome as get_latest_outcome
from .lessons import get_lesson_by_id as get_lesson_by_id
from .lessons import insert_lesson_evidence as insert_lesson_evidence
from .lessons import insert_lesson_outcome as insert_lesson_outcome
from .lessons import list_applied_lessons as list_applied_lessons
from .lessons import list_evidence_for_lessons as list_evidence_for_lessons
from .lessons import list_latest_outcomes as list_latest_outcomes
from .lessons import list_lesson_candidates as list_lesson_candidates
from .lessons import list_lesson_evidence as list_lesson_evidence
from .lessons import set_lesson_merged_commit_sha as set_lesson_merged_commit_sha
from .lessons import set_lesson_status_reason as set_lesson_status_reason
from .lessons import update_lesson_status as update_lesson_status
from .lessons import upsert_lesson_candidate as upsert_lesson_candidate
from .pr_runs import PrRunUpsert as PrRunUpsert
from .pr_runs import find_latest_merged_pr_run_by_ticket as find_latest_merged_pr_run_by_ticket
from .pr_runs import get_pr_run_by_unique as get_pr_run_by_unique
from .pr_runs import list_client_profiles as list_client_profiles
from .pr_runs import list_pr_runs as list_pr_runs
from .pr_runs import upsert_pr_run as upsert_pr_run
from .schema import AI_SOURCES as AI_SOURCES
from .schema import _current_schema_version as _current_schema_version
from .schema import _migrate_to_v1 as _migrate_to_v1
from .schema import _migrate_to_v2 as _migrate_to_v2
from .schema import _migrate_to_v3 as _migrate_to_v3
from .schema import _migrate_to_v4 as _migrate_to_v4
from .schema import _migrate_to_v5 as _migrate_to_v5
from .schema import _now_iso as _now_iso
from .schema import autonomy_conn as autonomy_conn
from .schema import ensure_schema as ensure_schema
from .schema import get_autonomy_conn as get_autonomy_conn
from .schema import logger as logger
from .schema import open_connection as open_connection
from .schema import resolve_db_path as resolve_db_path
