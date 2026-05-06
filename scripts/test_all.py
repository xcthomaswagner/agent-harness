#!/usr/bin/env python3
"""Run all harness validation suites with service-local pytest roots."""

from __future__ import annotations

import argparse
import importlib.util
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class TestStep:
    name: str
    cwd: Path
    command: tuple[str, ...]
    required_modules: tuple[str, ...] = ()
    install_hint: str = ""


def build_steps(
    *,
    skip_root: bool = False,
    skip_l1: bool = False,
    skip_l3: bool = False,
    skip_ui: bool = False,
    skip_ui_build: bool = False,
) -> list[TestStep]:
    steps: list[TestStep] = []

    if not skip_root:
        steps.append(
            TestStep(
                name="root pytest",
                cwd=REPO_ROOT,
                command=(sys.executable, "-m", "pytest", "-q"),
            )
        )

    if not skip_l1:
        steps.append(
            TestStep(
                name="L1 pytest",
                cwd=REPO_ROOT / "services" / "l1_preprocessing",
                command=(sys.executable, "-m", "pytest", "-q"),
            )
        )

    if not skip_l3:
        steps.append(
            TestStep(
                name="L3 pytest",
                cwd=REPO_ROOT / "services" / "l3_pr_review",
                command=(sys.executable, "-m", "pytest", "-q"),
                required_modules=("respx",),
                install_hint='python -m pip install -e "services/l3_pr_review[dev]"',
            )
        )

    if not skip_ui:
        ui_dir = REPO_ROOT / "services" / "operator_ui"
        steps.extend(
            [
                TestStep(
                    name="operator UI typecheck",
                    cwd=ui_dir,
                    command=("npm", "run", "typecheck"),
                ),
                TestStep(
                    name="operator UI tests",
                    cwd=ui_dir,
                    command=("npm", "test", "--", "--run"),
                ),
            ]
        )
        if not skip_ui_build:
            steps.append(
                TestStep(
                    name="operator UI build",
                    cwd=ui_dir,
                    command=("npm", "run", "build"),
                )
            )

    return steps


def missing_modules(step: TestStep) -> tuple[str, ...]:
    return tuple(
        module for module in step.required_modules if importlib.util.find_spec(module) is None
    )


def format_command(step: TestStep) -> str:
    try:
        cwd = step.cwd.relative_to(REPO_ROOT)
    except ValueError:
        cwd = step.cwd
    rendered = " ".join(shlex.quote(part) for part in step.command)
    return f"cd {shlex.quote(str(cwd))} && {rendered}"


def run_step(step: TestStep, *, dry_run: bool = False) -> int:
    print(f"\n==> {step.name}", flush=True)
    print(format_command(step), flush=True)
    if dry_run:
        return 0

    missing = missing_modules(step)
    if missing:
        print(
            f"ERROR: {step.name} requires missing Python module(s): {', '.join(missing)}",
            file=sys.stderr,
        )
        if step.install_hint:
            print(f"Install with: {step.install_hint}", file=sys.stderr)
        return 2

    completed = subprocess.run(step.command, cwd=step.cwd, check=False)
    return completed.returncode


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the repo validation suites from the working directories their "
            "pytest/npm configs expect."
        )
    )
    parser.add_argument("--skip-root", action="store_true", help="Skip root-level tests.")
    parser.add_argument("--skip-l1", action="store_true", help="Skip L1 service tests.")
    parser.add_argument("--skip-l3", action="store_true", help="Skip L3 service tests.")
    parser.add_argument("--skip-ui", action="store_true", help="Skip operator UI checks.")
    parser.add_argument("--skip-ui-build", action="store_true", help="Skip operator UI build.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    steps = build_steps(
        skip_root=args.skip_root,
        skip_l1=args.skip_l1,
        skip_l3=args.skip_l3,
        skip_ui=args.skip_ui,
        skip_ui_build=args.skip_ui_build,
    )

    for step in steps:
        result = run_step(step, dry_run=args.dry_run)
        if result != 0:
            return result

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
