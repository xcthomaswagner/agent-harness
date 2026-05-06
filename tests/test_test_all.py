from pathlib import Path
import importlib.util
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "scripts" / "test_all.py"
SPEC = importlib.util.spec_from_file_location("agent_harness_test_all", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
test_all = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = test_all
SPEC.loader.exec_module(test_all)


def _by_name(steps: list[test_all.TestStep]) -> dict[str, test_all.TestStep]:
    return {step.name: step for step in steps}


def test_default_steps_keep_service_scoped_pytest_roots() -> None:
    steps = _by_name(test_all.build_steps())

    assert steps["root pytest"].cwd == test_all.REPO_ROOT
    assert steps["root pytest"].command[-2:] == ("pytest", "-q")
    assert steps["L1 pytest"].cwd == test_all.REPO_ROOT / "services" / "l1_preprocessing"
    assert steps["L3 pytest"].cwd == test_all.REPO_ROOT / "services" / "l3_pr_review"
    assert steps["L3 pytest"].required_modules == ("respx",)


def test_skip_flags_remove_selected_suites() -> None:
    steps = test_all.build_steps(skip_root=True, skip_l3=True, skip_ui=True)

    assert [step.name for step in steps] == ["L1 pytest"]


def test_skip_ui_build_keeps_typecheck_and_tests() -> None:
    steps = [step.name for step in test_all.build_steps(skip_ui_build=True)]

    assert "operator UI typecheck" in steps
    assert "operator UI tests" in steps
    assert "operator UI build" not in steps


def test_missing_dependency_reports_before_subprocess(capsys) -> None:
    step = test_all.TestStep(
        name="missing dependency suite",
        cwd=Path("/tmp"),
        command=("python", "-c", "raise SystemExit(99)"),
        required_modules=("definitely_missing_agent_harness_test_dep",),
        install_hint="install test dependency",
    )

    assert test_all.run_step(step) == 2

    captured = capsys.readouterr()
    assert "definitely_missing_agent_harness_test_dep" in captured.err
    assert "install test dependency" in captured.err
