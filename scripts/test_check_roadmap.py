import contextlib
import io
from pathlib import Path
import tempfile
import unittest

import check_roadmap
import check_roadmap_dependencies as dependencies


def roadmap_entry(number="900", title="Example", status="planned", fields=None):
    values = fields or {
        "Description": "A test entry",
        "Outcome": "A validated roadmap",
        "Scope (in)": "Format validation",
        "Depends on": "none",
    }
    return (
        f"### {number} — {title}  [status: {status}]\n"
        + "".join(f"- **{name}:** {value}\n" for name, value in values.items())
    )


class CheckRoadmapTest(unittest.TestCase):
    def test_real_entries_outside_planned_section_are_rejected(self):
        for text in (
            roadmap_entry("901") + "## Planned Specs\n" + roadmap_entry(),
            "## Planned Specs\n" + roadmap_entry() + "## Archive\n" + roadmap_entry("901"),
            "## Planned Specs\n" + roadmap_entry() + "# Appendix\n" + roadmap_entry("901"),
        ):
            with self.subTest(text=text), self.assertRaisesRegex(
                dependencies.InvalidData, "outside Planned Specs"
            ):
                check_roadmap.validate_roadmap_text(text)

    def test_outside_fenced_and_commented_examples_are_not_entries(self):
        text = (
            "```markdown\n" + roadmap_entry("901") + "```\n"
            "<!--\n" + roadmap_entry("902") + "-->\n"
            "## Planned Specs\n" + roadmap_entry()
        )
        self.assertEqual(set(check_roadmap.validate_roadmap_text(text)), {"900"})

    def test_valid_plan_entries_parse_with_only_plan_statuses(self):
        text = (
            "## Planned Specs\n\n"
            + roadmap_entry()
            + "\n"
            + roadmap_entry(
                "901",
                "Dependent",
                fields={
                    "Description": "Uses the first entry",
                    "Outcome": "A complete graph",
                    "Scope (in)": "Dependencies",
                    "Depends on": "900",
                    "Spec dir": "specs/901-dependent/",
                },
            )
        )
        entries = check_roadmap.validate_roadmap_text(text)
        self.assertEqual(set(entries), {"900", "901"})
        self.assertEqual(entries["901"]["depends"], ["900"])

    def test_malformed_entry_heading_fails_with_line_number(self):
        text = "## Planned Specs\n\n### 900 Example [status: planned]\n"
        with self.assertRaisesRegex(dependencies.InvalidData, r"line 3:.*heading"):
            check_roadmap.validate_roadmap_text(text)

    def test_duplicate_ids_fail(self):
        text = "## Planned Specs\n\n" + roadmap_entry() + "\n" + roadmap_entry()
        with self.assertRaisesRegex(dependencies.InvalidData, "duplicate entry ID 900"):
            check_roadmap.validate_roadmap_text(text)

    def test_non_plan_status_fails(self):
        text = "## Planned Specs\n\n" + roadmap_entry(status="implemented")
        with self.assertRaisesRegex(dependencies.InvalidData, "invalid status 'implemented'"):
            check_roadmap.validate_roadmap_text(text)

    def test_missing_required_field_fails(self):
        text = "## Planned Specs\n\n" + roadmap_entry(
            fields={
                "Description": "A test entry",
                "Outcome": "A validated roadmap",
                "Depends on": "none",
            }
        )
        with self.assertRaisesRegex(dependencies.InvalidData, "missing required field 'Scope \\(in\\)'"):
            check_roadmap.validate_roadmap_text(text)

    def test_unknown_or_malformed_field_fails_instead_of_being_ignored(self):
        for field in ("Unknown field", "Description"):
            extra = (
                f"- **{field}:** unknown\n"
                if field == "Unknown field"
                else "- **Description** missing colon\n"
            )
            entry = roadmap_entry().replace(
                "- **Depends on:** none\n",
                extra + "- **Depends on:** none\n",
            )
            malformed = "## Planned Specs\n\n" + entry
            with self.subTest(field=field):
                with self.assertRaisesRegex(dependencies.InvalidData, r"line \d+:.*field"):
                    check_roadmap.validate_roadmap_text(malformed)

    def test_history_field_is_rejected_as_unknown(self):
        entry = roadmap_entry().replace(
            "- **Depends on:** none\n",
            "- **History:** old\n- **Depends on:** none\n",
        )
        with self.assertRaisesRegex(dependencies.InvalidData, r"line \d+:.*History"):
            check_roadmap.validate_roadmap_text("## Planned Specs\n\n" + entry)

    def test_unknown_dependency_fails(self):
        text = "## Planned Specs\n\n" + roadmap_entry(
            fields={
                "Description": "A test entry",
                "Outcome": "A validated roadmap",
                "Scope (in)": "Format validation",
                "Depends on": "901",
            }
        )
        with self.assertRaisesRegex(dependencies.InvalidData, "dependency 901 does not exist"):
            check_roadmap.validate_roadmap_text(text)

    def test_dependency_cycle_fails(self):
        first = roadmap_entry(
            "900",
            fields={
                "Description": "First",
                "Outcome": "First outcome",
                "Scope (in)": "First scope",
                "Depends on": "901",
            },
        )
        second = roadmap_entry(
            "901",
            fields={
                "Description": "Second",
                "Outcome": "Second outcome",
                "Scope (in)": "Second scope",
                "Depends on": "900",
            },
        )
        with self.assertRaisesRegex(dependencies.InvalidData, "dependency cycle"):
            check_roadmap.validate_roadmap_text("## Planned Specs\n\n" + first + second)

    def test_spec_dir_prefix_must_match_entry_id(self):
        text = "## Planned Specs\n\n" + roadmap_entry(
            fields={
                "Description": "A test entry",
                "Outcome": "A validated roadmap",
                "Scope (in)": "Format validation",
                "Depends on": "none",
                "Spec dir": "specs/901-wrong/",
            }
        )
        with self.assertRaisesRegex(dependencies.InvalidData, "Spec dir prefix"):
            check_roadmap.validate_roadmap_text(text)

    def test_cli_validates_the_repository_roadmap_and_spec_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ROADMAP.md").write_text(
                "## Planned Specs\n\n" + roadmap_entry(), encoding="utf-8"
            )
            (root / "specs").mkdir()
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                result = check_roadmap.main(["--root", str(root)])
            self.assertEqual(result, 0, errors.getvalue())
            self.assertIn("Roadmap format is valid", output.getvalue())


    def test_spec_id_errors_fail_validation_and_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ROADMAP.md").write_text(
                "## Planned Specs\n\n" + roadmap_entry(), encoding="utf-8"
            )
            spec_dir = root / "specs" / "900-x"
            spec_dir.mkdir(parents=True)
            (spec_dir / "spec.md").write_text(
                "**Input**: `ROADMAP.md` entry 901\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(
                dependencies.InvalidData, "directory prefix does not match roadmap entry 901"
            ):
                check_roadmap.validate_roadmap(root)
            errors = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(errors):
                result = check_roadmap.main(["--root", str(root)])
            self.assertEqual(result, 1)
            self.assertIn("directory prefix does not match roadmap entry 901", errors.getvalue())


class UninitializedRepositoryTest(unittest.TestCase):
    def run_main(self, *files):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir(exist_ok=True)
            (root / "scripts" / "project_context.py").write_text("", encoding="utf-8")
            for name in files:
                (root / name).mkdir(parents=True)
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = check_roadmap.main(["--root", str(root)])
        return code, output.getvalue(), errors.getvalue()

    def test_an_uninitialized_repository_passes_with_one_clear_line(self):
        code, output, errors = self.run_main()
        self.assertEqual(code, 0, errors)
        self.assertEqual(output, "Not initialized yet: no ROADMAP.md. See docs/bootstrap.md.\n")

    def test_an_initialized_repository_without_a_roadmap_fails_clearly(self):
        code, output, errors = self.run_main("specs/001-x")
        self.assertEqual(code, 1)
        self.assertEqual(output, "")
        self.assertIn("ROADMAP.md is missing", errors)
        self.assertIn("`roadmap-write` skill", errors)
        self.assertIn("docs/bootstrap.md", errors)

    def test_a_mistyped_root_is_an_error_not_uninitialized(self):
        with tempfile.TemporaryDirectory() as directory:
            typo = Path(directory) / "typo"
            errors = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(errors):
                code = check_roadmap.main(["--root", str(typo)])
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("not a repository root", errors.getvalue())

    def test_the_constitution_alone_makes_the_repository_initialized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts" / "project_context.py").write_text("", encoding="utf-8")
            (root / ".specify/memory").mkdir(parents=True)
            (root / ".specify/memory/constitution.md").write_text("", encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
                code = check_roadmap.main(["--root", str(root)])
        self.assertEqual(code, 1)
        self.assertIn("ROADMAP.md is missing", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
