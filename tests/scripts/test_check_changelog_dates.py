"""Offline regression tests, runnable with unittest without application setup."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/check_changelog_dates.py"
SPEC = importlib.util.spec_from_file_location("check_changelog_dates", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)


def release(tag: str = "v1.2.3", published: object = "2026-04-05T22:50:27Z", draft: bool = False) -> dict:
    return {"tag_name": tag, "published_at": published, "draft": draft, "prerelease": False}


class ChangelogDatesTests(unittest.TestCase):
    def assert_workflow_contract(self, text: str) -> None:
        self.assertRegex(
            text,
            r"(?m)^on:\n  schedule:\n    - cron: [\"\']35 9 \* \* \*[\"\']\n  workflow_dispatch:\n\npermissions:",
        )
        self.assertRegex(text, r"(?m)^permissions:\n  contents: read\n\njobs:")
        self.assertNotRegex(text, r"(?m)^    [\"']?permissions[\"']?:")
        self.assertNotRegex(text, r"(?m)^ +(?:- +)?[\"']?(?:if|continue-on-error)[\"']?:")
        jobs = text.split("\njobs:\n")[1]
        self.assertEqual(re.findall(r"(?m)^  [^ \n]+:", jobs), ["  audit:"])
        self.assertRegex(text, r"(?m)^    timeout-minutes: [1-5]$")
        steps = text.split("\n      - ")[1:]
        self.assertEqual(len(steps), 4)
        for step, action in zip(steps[:2], ["checkout", "setup-python"], strict=True):
            self.assertRegex(step, rf"^uses: actions/{action}@[0-9a-f]{{40}}(?: #.*)?\n")
        self.assertIn("          persist-credentials: false\n", steps[0])
        self.assertIn('          python-version: "3.11"\n', steps[1])
        self.assertRegex(steps[2], r"(?m)^        run: python tests/scripts/test_check_changelog_dates\.py -v$")
        self.assertRegex(steps[3], r"(?m)^        run: python scripts/check_changelog_dates\.py$")
        self.assertIn("        env:\n          GH_TOKEN: ${{ github.token }}\n", steps[3])
        self.assertEqual(text.count("GH_TOKEN:"), 1)
        self.assertEqual(len(re.findall(r"\b(?:github\.token|secrets\.GITHUB_TOKEN)\b", text)), 1)

    def test_workflow_contract(self) -> None:
        workflow = SCRIPT.parents[1] / ".github/workflows/changelog-dates.yml"
        self.assert_workflow_contract(workflow.read_text(encoding="utf-8"))

    def test_workflow_contract_rejects_drift(self) -> None:
        workflow = SCRIPT.parents[1] / ".github/workflows/changelog-dates.yml"
        text = workflow.read_text(encoding="utf-8")
        blocks = text.split("\n      - ")
        mutations = {
            "schedule": text.replace('  schedule:\n    - cron: "35 9 * * *"\n', ""),
            "manual": text.replace("  workflow_dispatch:\n", ""),
            "permissions": text.replace("contents: read", "contents: write"),
            "job permissions": text.replace("  audit:\n", "  audit:\n    permissions: write-all\n"),
            "quoted job permissions": text.replace("  audit:\n", '  audit:\n    "permissions": write-all\n'),
            "single-quoted job permissions": text.replace("  audit:\n", "  audit:\n    'permissions': write-all\n"),
            "action pin": re.sub(r"actions/checkout@[0-9a-f]{40}", "actions/checkout@v7", text),
            "timeout": text.replace("    timeout-minutes: 5\n", ""),
            "credentials": text.replace("persist-credentials: false", "persist-credentials: true"),
            "python": text.replace('python-version: "3.11"', 'python-version: "3.10"'),
            "ordering": "\n      - ".join([*blocks[:3], blocks[4], blocks[3]]),
            "token": text.replace("          GH_TOKEN: ${{ github.token }}\n", ""),
            "token isolation": text.replace("          GH_TOKEN: ${{ github.token }}\n", "").replace(
                "jobs:\n", "env:\n  GH_TOKEN: ${{ github.token }}\n\njobs:\n"
            ),
            "job ignores errors": text.replace("  audit:\n", "  audit:\n    continue-on-error: true\n"),
            "step ignores errors": text.replace(
                "Run offline self-tests\n", "Run offline self-tests\n        continue-on-error: true\n"
            ),
            "conditional job": text.replace("  audit:\n", "  audit:\n    if: github.event_name == 'push'\n"),
            "conditional step": text.replace(
                "Run offline self-tests\n", "Run offline self-tests\n        if: github.event_name == 'push'\n"
            ),
            "step-first condition": text.replace(
                "      - name: Run offline self-tests\n",
                "      - if: github.event_name == 'push'\n        name: Run offline self-tests\n",
            ),
            "step-first ignored error": text.replace(
                "      - name: Audit published release dates\n",
                "      - continue-on-error: true\n        name: Audit published release dates\n",
            ),
            "token alias": text.replace(
                "  audit:\n", "  audit:\n    env:\n      AUDIT_TOKEN: ${{ secrets.GITHUB_TOKEN }}\n"
            ),
        }
        for name, changed in mutations.items():
            with self.subTest(guard=name), self.assertRaises(AssertionError):
                self.assert_workflow_contract(changed)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="changelog dates ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fixture = self.root / "releases.json"

    def inputs(
        self,
        data: object = None,
        root_heading: str = "## [1.2.3] - 2026-04-06",
        addon_heading: str = "## 1.2.3 - 2026-04-06",
    ) -> list[str]:
        self.fixture.write_text(json.dumps([release()] if data is None else data), encoding="utf-8")
        (self.root / "CHANGELOG.md").write_text(root_heading + "\nRelease prose.\n", encoding="utf-8")
        addon = self.root / "ha-addon/mammamiradio/CHANGELOG.md"
        addon.parent.mkdir(parents=True, exist_ok=True)
        addon.write_text(addon_heading + "\nRelease prose.\n", encoding="utf-8")
        return ["--repo-root", str(self.root), "--releases-json", str(self.fixture)]

    def run_main(self, args: list[str]) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = watch.main(args)
        return code, stdout.getvalue(), stderr.getvalue()

    def cli(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    def test_matching_cli_audits_both_files_without_mutating_them(self) -> None:
        args = self.inputs()
        before = [(self.root / name).read_bytes() for name in watch.CHANGELOGS]
        result = self.cli(args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Europe/Berlin", result.stdout)
        for name in watch.CHANGELOGS:
            self.assertIn(f"{name}: 1 checked, 0 skipped", result.stdout)
        self.assertEqual(before, [(self.root / name).read_bytes() for name in watch.CHANGELOGS])

    def test_date_mutation_fails_the_actual_cli_for_either_file(self) -> None:
        for root, addon, name in [
            ("## [1.2.3] - 2026-04-05", "## 1.2.3 - 2026-04-06", "CHANGELOG.md"),
            ("## [1.2.3] - 2026-04-06", "## 1.2.3 - 2026-04-05", watch.CHANGELOGS[1]),
        ]:
            with self.subTest(file=name):
                result = self.cli(self.inputs(root_heading=root, addon_heading=addon))
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(f"{name}:1: 1.2.3: date 2026-04-05; expected 2026-04-06", result.stdout)

    def test_all_missing_invalid_and_duplicate_dates_are_reported(self) -> None:
        root = (
            "## [1.2.3]\n## [1.2.3] - 2026-02-30\n## [1.2.3] - someday\n"
            "## [1.2.3] - 2026-04-06\n## [1.2.3] -  2026-04-06"
        )
        code, stdout, _ = self.run_main(self.inputs(root_heading=root, addon_heading="## 1.2.3"))
        self.assertEqual(code, 1)
        for line, actual in [(1, "<missing>"), (2, "2026-02-30"), (3, "someday")]:
            self.assertIn(f"CHANGELOG.md:{line}: 1.2.3: date {actual}; expected 2026-04-06", stdout)
        self.assertIn("heading suffix '-  2026-04-06'", stdout)
        self.assertIn("Found 5 date finding(s)", stdout)

    def test_publication_time_not_creation_time_and_local_timezone(self) -> None:
        data = release("v2.0.1")
        data["created_at"] = "2026-04-04T10:00:00Z"
        with patch.dict("os.environ", {"TZ": "Pacific/Honolulu"}):
            dates = watch.release_dates([data])
        self.assertEqual(dates["2.0.1"].isoformat(), "2026-04-06")

    def test_summer_winter_midday_and_offset_boundaries(self) -> None:
        cases = [
            ("2026-04-05T22:50:27Z", "2026-04-06"),
            ("2026-04-06T00:50:27+02:00", "2026-04-06"),
            ("2026-01-05T23:30:00Z", "2026-01-06"),
            ("2026-01-05T22:30:00Z", "2026-01-05"),
            ("2026-04-05T12:00:00Z", "2026-04-05"),
        ]
        for published, expected in cases:
            with self.subTest(published=published):
                self.assertEqual(watch.release_dates([release(published=published)])["1.2.3"].isoformat(), expected)

    def test_drafts_unpublished_and_unreleased_headings_are_skipped(self) -> None:
        root = "## [Unreleased]\n## [9.9.9]\n## [1.2.3] - 2026-04-06"
        addon = "## Unreleased\n## 1.2.3 - 2026-04-06"
        code, stdout, _ = self.run_main(self.inputs([release(), release("v9.9.9", None, True)], root, addon))
        self.assertEqual(code, 0)
        self.assertIn("CHANGELOG.md: 1 checked, 2 skipped", stdout)
        self.assertIn(f"{watch.CHANGELOGS[1]}: 1 checked, 1 skipped", stdout)

    def test_prerelease_and_stable_keys_are_distinct_and_addon_can_be_a_subset(self) -> None:
        data = [release(), release("1.2.3-beta.1", "2026-04-04T12:00:00Z"), release("abcdef1")]
        data[1]["prerelease"] = True
        root = "## [1.2.3] - 2026-04-06\n## [1.2.3-beta.1] - 2026-04-04"
        code, stdout, _ = self.run_main(self.inputs(data, root))
        self.assertEqual(code, 0)
        self.assertIn("CHANGELOG.md: 2 checked", stdout)
        self.assertIn(f"{watch.CHANGELOGS[1]}: 1 checked", stdout)

    def test_paginated_and_flat_inputs_produce_the_same_complete_audit(self) -> None:
        releases = [release(), release("v2.0.1")]
        root = "## [1.2.3] - 2026-04-06\n## [2.0.1] - 2026-04-05"
        flat = self.run_main(self.inputs(releases, root))
        pages = self.run_main(self.inputs([[releases[0]], [releases[1]]], root))
        self.assertEqual(flat, pages)
        self.assertEqual(pages[0], 1)
        self.assertIn("2.0.1: date 2026-04-05; expected 2026-04-06", pages[1])

    def test_duplicate_published_tags_do_not_select_an_arbitrary_date(self) -> None:
        code, stdout, stderr = self.run_main(self.inputs([release(), release("1.2.3")]))
        self.assertEqual(code, 2)
        self.assertIn("multiple published releases", stderr)
        self.assertNotIn("dates match", stdout)

    def test_empty_malformed_and_mixed_json_never_pass(self) -> None:
        for data in [[], [[]], {}, [None], [[release()], release()], [release(draft=True)], [release("abcdef1")]]:
            with self.subTest(data=data):
                code, stdout, stderr = self.run_main(self.inputs(data))
                self.assertEqual(code, 2)
                self.assertIn("ERROR:", stderr)
                self.assertNotIn("dates match", stdout)

    def test_invalid_release_fields_and_naive_timestamps_never_pass(self) -> None:
        rows = [release(published=p) for p in [None, 123, "2026-04-05", "2026-04-05T22:50:27", "bad"]]
        rows += [release(published="2026-02-30T12:00:00Z"), {"tag_name": "v1.2.3"}, release() | {"draft": "false"}]
        rows += [release() | {"tag_name": None}, release() | {"tag_name": ""}]
        for row in rows:
            with self.subTest(row=row):
                code, stdout, _ = self.run_main(self.inputs([release("v2.0.1"), row]))
                self.assertEqual(code, 2)
                self.assertNotIn("dates match", stdout)

    def test_zero_comparable_headings_in_either_file_never_pass(self) -> None:
        for name in watch.CHANGELOGS:
            with self.subTest(file=name):
                args = self.inputs()
                (self.root / name).write_text("## Unreleased\n## 9.9.9\n", encoding="utf-8")
                code, stdout, stderr = self.run_main(args)
                self.assertEqual(code, 2)
                self.assertIn(f"{name}: no headings match", stderr)
                self.assertNotIn("dates match", stdout)

    def test_missing_or_unreadable_inputs_override_findings(self) -> None:
        for name in watch.CHANGELOGS:
            with self.subTest(file=name):
                args = self.inputs(root_heading="## [1.2.3] - 2026-04-05")
                (self.root / name).unlink()
                result = self.cli(args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("ERROR:", result.stderr)
                self.assertNotIn("dates match", result.stdout)
        args = self.inputs()
        with (
            patch.object(watch, "load_releases", return_value=[release()]),
            patch.object(watch.Path, "read_text", side_effect=PermissionError("unreadable")),
        ):
            self.assertEqual(self.run_main(args)[0], 2)

    def test_missing_bad_json_and_bad_utf8_fixture_never_pass(self) -> None:
        for content in [None, b"not JSON", b"\xff"]:
            with self.subTest(content=content):
                args = self.inputs()
                if content is None:
                    self.fixture.unlink()
                else:
                    self.fixture.write_bytes(content)
                self.assertEqual(self.cli(args).returncode, 2)
        args = self.inputs()
        (self.root / watch.CHANGELOGS[1]).write_bytes(b"\xff")
        self.assertEqual(self.cli(args).returncode, 2)

    def test_malformed_version_heading_cannot_disappear(self) -> None:
        args = self.inputs(root_heading="## [1.2.3] - 2026-04-06\n## [2.0.1 - 2026-04-05")
        code, _, stderr = self.run_main(args)
        self.assertEqual(code, 2)
        self.assertIn("CHANGELOG.md:2: unrecognized release heading", stderr)

    def test_indented_release_headings_cannot_hide_findings_in_either_file(self) -> None:
        for name in watch.CHANGELOGS:
            for spaces in (1, 2, 3):
                with self.subTest(file=name, spaces=spaces):
                    args = self.inputs()
                    path = self.root / name
                    version = "[1.2.3]" if name == "CHANGELOG.md" else "1.2.3"
                    path.write_text(
                        f"## {version} - 2026-04-06\n{' ' * spaces}## {version} - 1900-01-01\n",
                        encoding="utf-8",
                    )
                    result = self.cli(args)
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn(f"{name}:2: 1.2.3: date 1900-01-01; expected 2026-04-06", result.stdout)

    def test_fenced_examples_are_not_release_headings_and_crlf_is_supported(self) -> None:
        root = "```md\n## [1.2.3] - 1900-01-01\n```\n~~~\n## example\n~~~\n## [1.2.3] - 2026-04-06 \r"
        self.assertEqual(self.run_main(self.inputs(root_heading=root))[0], 0)

    def test_offline_loading_never_calls_gh(self) -> None:
        args = self.inputs([[release()]])
        with patch.object(watch.subprocess, "run", side_effect=AssertionError("network called")):
            self.assertEqual(self.run_main(args)[0], 0)

    def test_live_fetch_pins_the_source_and_has_a_total_timeout(self) -> None:
        args = self.inputs()[:2]
        response = subprocess.CompletedProcess([], 0, json.dumps([[release()]]))
        with (
            patch.dict("os.environ", {"GH_HOST": "unrelated.example"}),
            patch.object(watch.subprocess, "run", return_value=response) as fetch,
        ):
            self.assertEqual(self.run_main(args)[0], 0)
        command = fetch.call_args.args[0]
        self.assertEqual(command[:6], ["gh", "api", "--hostname", "github.com", "--method", "GET"])
        self.assertEqual(command[-1], "repos/florianhorner/mammamiradio/releases?per_page=100")
        self.assertIn("--paginate", command)
        self.assertIn("--slurp", command)
        self.assertNotIn("--jq", command)
        self.assertEqual(fetch.call_args.kwargs["timeout"], 30)
        self.assertNotIn("shell", fetch.call_args.kwargs)

    def test_live_source_failures_are_distinct_from_date_findings(self) -> None:
        args = self.inputs()[:2]
        failures = [
            FileNotFoundError("gh"),
            OSError("unavailable"),
            subprocess.TimeoutExpired("gh", 30),
            subprocess.CalledProcessError(1, "gh", stderr="private-sentinel rate limit"),
        ]
        for failure in failures:
            with self.subTest(failure=failure):
                with patch.object(watch.subprocess, "run", side_effect=failure):
                    code, stdout, stderr = self.run_main(args)
                self.assertEqual(code, 2)
                self.assertIn("--help", stderr)
                self.assertNotIn("private-sentinel", stderr)
                self.assertNotIn("dates match", stdout)
        with patch.object(watch.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "bad JSON")):
            self.assertEqual(self.run_main(args)[0], 2)

    def test_missing_timezone_data_fails_with_a_remedy_but_help_needs_no_io(self) -> None:
        args = self.inputs()
        with patch.object(watch, "ZoneInfo", side_effect=watch.ZoneInfoNotFoundError("missing")):
            code, _, stderr = self.run_main(args)
            self.assertEqual(code, 2)
            self.assertIn("install system tzdata", stderr)
            with patch.object(watch, "load_releases", side_effect=AssertionError("I/O called")):
                with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as exit_info:
                    watch.main(["--help", "--repo-root", "/missing"])
                self.assertEqual(exit_info.exception.code, 0)
        result = self.cli(["--help", "--repo-root", "/missing", "--releases-json", "/missing"])
        self.assertEqual(result.returncode, 0)
        self.assertIn("Exit 0:", result.stdout)


if __name__ == "__main__":
    unittest.main()
