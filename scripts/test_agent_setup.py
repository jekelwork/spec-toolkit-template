import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


SCRIPT_PATH = Path(__file__).with_name("agent_setup.py")
SPEC = importlib.util.spec_from_file_location("agent_setup", SCRIPT_PATH)
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)

VERSION = "1.2.0"
GENERATED_SKILL = ".agents/skills/speckit-plan/SKILL.md"
GENERATED_SCRIPT = ".specify/scripts/bash/check-prerequisites.sh"
AGENT = """---
name: code-reviewer
description: Reviews "diffs" before merge.
tools: [Read, Grep]
---

# Code reviewer
"""
CONFIG = """[agents]
code-reviewer = "review"

[profiles.review.claude]
model = "opus"
effort = "medium"
[profiles.review.copilot]
model = "gpt-5"
effort = "high"
[profiles.review.codex]
model = "gpt-5-codex"
effort = "low"
"""
SOURCE = "agent-definitions/code-reviewer.md"
CONFIG_PATH = "agent-definitions/agents.toml"


def generated_content(path):
    marker = setup.GENERATED_MARKER
    if path.endswith(".md"):
        return f"---\n{marker} from x. Do not edit.\nname: old\n---\n"
    return f"{marker} from x. Do not edit.\nname = \"old\"\n"


def write(root, path, content):
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content, encoding="utf-8")


def write_json(root, path, data):
    write(root, path, json.dumps(data))


class AgentSetupTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        # Keep a developer's own `agent-setup.tools` git config out of the tests.
        environ = mock.patch.dict(
            os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        )
        environ.start()
        self.addCleanup(environ.stop)
        self.build_consistent_repo()

    def build_consistent_repo(self):
        root = self.root
        self.write_integration_state(["codex"], "codex")
        write_json(root, ".specify/init-options.json", {"speckit_version": VERSION})
        write(root, GENERATED_SKILL, "generated\n")
        write(root, GENERATED_SCRIPT, "#!/usr/bin/env bash\n")
        (root / GENERATED_SCRIPT).chmod(0o755)
        self.write_manifest()
        write_json(
            root,
            ".specify/extensions/.registry",
            {"extensions": {"roadmap": {"registered_skills": ["speckit-roadmap-sync"]}}},
        )
        write(root, ".agents/skills/speckit-roadmap-sync/SKILL.md", "roadmap\n")
        write(root, "agent-definitions/code-reviewer.md", AGENT)
        write(root, "agent-definitions/agents.toml", CONFIG)
        (root / ".claude").mkdir()
        os.symlink("../.agents/skills", root / ".claude/skills")
        with contextlib.redirect_stdout(io.StringIO()):
            setup.run_sync(root)

    def write_integration_state(self, installed, default):
        write_json(
            self.root,
            ".specify/integration.json",
            {
                "version": VERSION,
                "installed_integrations": installed,
                "default_integration": default,
            },
        )

    def write_manifest(self, version=VERSION):
        files = {
            path: hashlib.sha256((self.root / path).read_bytes()).hexdigest()
            for path in (GENERATED_SKILL, GENERATED_SCRIPT)
        }
        write_json(
            self.root,
            ".specify/integrations/codex.manifest.json",
            {"version": version, "files": files},
        )

    def problems(self):
        return setup.find_problems(self.root)

    def assert_single_problem(self, fragment):
        problems = self.problems()
        self.assertEqual(1, len(problems), problems)
        self.assertIn(fragment, problems[0])

    def test_consistent_repo_passes(self):
        self.assertEqual([], self.problems())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, setup.run_check(self.root))

    def test_check_fails_and_reports_problem_as_ci_error(self):
        write(self.root, SOURCE, AGENT.replace("description: Reviews", "x: Reviews"))
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            self.assertEqual(1, setup.run_check(self.root))

        self.assertIn("::error::agent-definitions/code-reviewer.md: frontmatter needs 'description'", output.getvalue())

    def test_repo_without_generated_agent_files_passes(self):
        for folder in (".github/agents", ".claude/agents", ".codex/agents"):
            shutil.rmtree(self.root / folder)

        self.assertEqual([], self.problems())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, setup.run_check(self.root))

    def test_flags_manifest_from_another_version(self):
        self.write_manifest(version="1.3.0")

        self.assert_single_problem("codex.manifest.json: 1.3.0")

    def test_flags_init_options_from_another_version(self):
        write_json(self.root, ".specify/init-options.json", {"speckit_version": "1.1.0"})

        self.assert_single_problem("init-options.json: 1.1.0")

    def test_flags_second_installed_integration(self):
        self.write_integration_state(["codex", "copilot"], "codex")

        self.assert_single_problem("expected only 'codex'")

    def test_flags_other_default_integration(self):
        self.write_integration_state(["codex"], "copilot")

        self.assert_single_problem("default integration is 'copilot'")

    def test_flags_hand_edited_generated_file(self):
        write(self.root, GENERATED_SKILL, "generated, then edited\n")

        self.assert_single_problem("edited by hand")

    def test_flags_deleted_generated_file(self):
        (self.root / GENERATED_SKILL).unlink()

        self.assert_single_problem("missing (listed in codex.manifest.json)")

    def test_flags_missing_extension_skill(self):
        (self.root / ".agents/skills/speckit-roadmap-sync/SKILL.md").unlink()

        self.assert_single_problem("registered by the roadmap extension")

    def test_flags_entry_point_that_is_not_a_symlink(self):
        # What a Windows checkout without symlink support produces.
        (self.root / ".claude/skills").unlink()
        write(self.root, ".claude/skills", "../.agents/skills")

        self.assert_single_problem(".claude/skills: must be a symlink")

    def test_flags_symlink_to_wrong_target(self):
        (self.root / ".claude/skills").unlink()
        os.symlink("../skills", self.root / ".claude/skills")

        self.assert_single_problem("points to ../skills")

    def test_flags_github_skills_coming_back(self):
        write(self.root, ".github/skills/speckit-plan/SKILL.md", "copy\n")

        self.assert_single_problem(".github/skills: must not exist")

    def test_flags_github_skills_as_dangling_symlink(self):
        os.symlink("../missing", self.root / ".github/skills")

        self.assert_single_problem(".github/skills: must not exist")

    def test_flags_non_executable_generated_script(self):
        # Spec Kit 1.2.0 `integration switch` rewrites shared scripts without the executable bit.
        (self.root / GENERATED_SCRIPT).chmod(0o644)

        self.assert_single_problem("check-prerequisites.sh: not executable")

    def test_flags_non_executable_extension_script(self):
        # Extension scripts are not in an integration manifest but are run by extension skills.
        script = ".specify/extensions/roadmap/scripts/bash/load-config.sh"
        write(self.root, script, "#!/usr/bin/env bash\n")

        self.assert_single_problem("load-config.sh: not executable")

    def test_ignores_scripts_in_dot_folders(self):
        # Paths the script globs reach; only the dot-folder rule keeps them out.
        write(self.root, ".specify/extensions/.cache/scripts/bash/x.sh", "")
        write(self.root, ".specify/extensions/roadmap/scripts/.dev/x.sh", "")
        write(self.root, ".specify/scripts/.tmp/x.sh", "")

        self.assertEqual([], self.problems())

    def test_ignores_scripts_outside_shipped_script_folders(self):
        # The real layouts of the git-ignored cache and roadmap development material.
        write(self.root, ".specify/extensions/.cache/roadmap/scripts/bash/x.sh", "")
        write(self.root, ".specify/extensions/roadmap/.specify/scripts/bash/x.sh", "")

        self.assertEqual([], self.problems())

    def test_new_source_agent_passes_check_and_sync_generates_its_files(self):
        write(self.root, "agent-definitions/test-engineer.md", AGENT.replace("code-reviewer", "test-engineer"))
        write(self.root, CONFIG_PATH, CONFIG.replace('[agents]\n', '[agents]\ntest-engineer = "review"\n'))

        self.sync()
        self.assertEqual([], self.problems())
        for path in (
            ".github/agents/test-engineer.agent.md",
            ".claude/agents/test-engineer.md",
            ".codex/agents/test-engineer.toml",
        ):
            self.assertTrue((self.root / path).is_file(), path)

    def test_codex_pointer_is_valid_toml_with_settings(self):
        import tomllib

        content = (self.root / ".codex/agents/code-reviewer.toml").read_text(encoding="utf-8")
        pointer = tomllib.loads(content)

        self.assertEqual("code-reviewer", pointer["name"])
        self.assertEqual('Reviews "diffs" before merge.', pointer["description"])
        self.assertEqual("gpt-5-codex", pointer["model"])
        self.assertEqual("low", pointer["model_reasoning_effort"])
        self.assertIn(SOURCE, pointer["developer_instructions"])
        self.assertTrue(content.startswith("# Generated by `python3 scripts/agent_setup.py sync`"))

    def test_copilot_agent_has_source_frontmatter_plus_settings_in_fixed_order(self):
        content = (self.root / ".github/agents/code-reviewer.agent.md").read_text(encoding="utf-8")

        self.assertEqual(
            "---\n"
            "# Generated by `python3 scripts/agent_setup.py sync`; do not edit. "
            "Source of truth: agent-definitions/code-reviewer.md (instructions) and "
            "agent-definitions/agents.toml (model and effort per tool). "
            "Edit those, then run sync.\n"
            "name: code-reviewer\n"
            'description: Reviews "diffs" before merge.\n'
            "tools: [Read, Grep]\n"
            "model: gpt-5\n"
            "reasoning-effort: high\n"
            "---\n\n# Code reviewer\n",
            content,
        )

    def test_claude_agent_has_source_frontmatter_plus_settings_in_fixed_order(self):
        content = (self.root / ".claude/agents/code-reviewer.md").read_text(encoding="utf-8")

        self.assertIn("tools: [Read, Grep]\nmodel: opus\neffort: medium\n---\n\n# Code reviewer\n", content)
        self.assertNotIn("reasoning-effort", content)
        self.assertTrue(content.startswith("---\n# Generated by `python3 scripts/agent_setup.py sync`"))

    def test_missing_tool_settings_are_omitted(self):
        write(self.root, CONFIG_PATH, '[agents]\ncode-reviewer = "p"\n[profiles.p.claude]\nmodel = "sonnet"\n')
        with contextlib.redirect_stdout(io.StringIO()):
            setup.run_sync(self.root)

        copilot = (self.root / ".github/agents/code-reviewer.agent.md").read_text(encoding="utf-8")
        codex = (self.root / ".codex/agents/code-reviewer.toml").read_text(encoding="utf-8")
        claude = (self.root / ".claude/agents/code-reviewer.md").read_text(encoding="utf-8")

        self.assertNotIn("reasoning-effort", copilot)
        self.assertNotIn("model:", copilot)
        self.assertNotIn("model =", codex)
        self.assertNotIn("model_reasoning_effort", codex)
        self.assertIn("model: sonnet\n", claude)
        self.assertNotIn("\neffort:", claude)
        self.assertEqual([], self.problems())

    def sync(self, tools=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = setup.run_sync(self.root, tools)
        return code, output.getvalue()

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def generated_paths(self, tool=None):
        paths = {
            "copilot": ".github/agents/code-reviewer.agent.md",
            "claude": ".claude/agents/code-reviewer.md",
            "codex": ".codex/agents/code-reviewer.toml",
        }
        return [paths[tool]] if tool else list(paths.values())

    def existing_tools(self):
        return sorted(
            tool
            for tool in ("claude", "copilot", "codex")
            if (self.root / self.generated_paths(tool)[0]).exists()
        )

    def test_sync_with_tool_flag_generates_only_that_tool_and_removes_other_marked_files(self):
        code, output = self.sync(["copilot"])

        self.assertEqual(0, code)
        self.assertEqual(["copilot"], self.existing_tools())
        self.assertIn("removed .claude/agents/code-reviewer.md", output)
        self.assertIn("removed .codex/agents/code-reviewer.toml", output)

    def test_sync_removes_now_empty_folders_of_unselected_tools_and_prints_them(self):
        code, output = self.sync(["claude"])

        self.assertEqual(0, code)
        self.assertFalse((self.root / ".github/agents").exists())
        self.assertFalse((self.root / ".codex").exists())
        self.assertIn("removed .github/agents/\n", output)
        self.assertIn("removed .codex/agents/\n", output)
        self.assertIn("removed .codex/\n", output)
        self.assertTrue((self.root / ".claude/agents/code-reviewer.md").is_file())

    def test_sync_never_removes_github_or_claude_folders(self):
        write(self.root, ".github/workflows/toolkit.yml", "x\n")

        self.sync(["codex"])

        self.assertTrue((self.root / ".github/workflows/toolkit.yml").is_file())
        self.assertFalse((self.root / ".github/agents").exists())
        self.assertTrue((self.root / ".claude/skills").is_symlink())
        self.assertFalse((self.root / ".claude/agents").exists())
        self.assertTrue((self.root / ".claude").is_dir())

    def test_sync_keeps_folders_that_still_hold_something(self):
        write(self.root, ".codex/agents/mine.toml", 'name = "mine"\n')
        write(self.root, ".github/agents/notes.txt", "x\n")
        write(self.root, ".codex/config.toml", "x\n")

        _, output = self.sync(["claude"])

        self.assertTrue((self.root / ".codex/agents/mine.toml").is_file())
        self.assertTrue((self.root / ".github/agents/notes.txt").is_file())
        self.assertNotIn("removed .codex/agents/\n", output)
        self.assertNotIn("removed .github/agents/\n", output)

    def test_sync_keeps_codex_folder_when_it_holds_other_files(self):
        write(self.root, ".codex/config.toml", "x\n")

        _, output = self.sync(["claude"])

        self.assertFalse((self.root / ".codex/agents").exists())
        self.assertTrue((self.root / ".codex/config.toml").is_file())
        self.assertNotIn("removed .codex/\n", output)

    def test_sync_does_not_create_folders_of_unselected_tools(self):
        shutil.rmtree(self.root / ".github/agents")
        shutil.rmtree(self.root / ".codex")

        self.sync(["claude"])

        self.assertFalse((self.root / ".github/agents").exists())
        self.assertFalse((self.root / ".codex").exists())

    def test_sync_creates_folders_of_selected_tools(self):
        shutil.rmtree(self.root / ".github/agents")
        shutil.rmtree(self.root / ".codex")

        self.sync(["copilot", "codex"])

        self.assertTrue((self.root / ".github/agents").is_dir())
        self.assertTrue((self.root / ".codex/agents").is_dir())

    def test_sync_removes_preexisting_empty_folder_of_unselected_tool_once(self):
        self.sync(["claude"])
        (self.root / ".codex/agents").mkdir(parents=True)

        _, first = self.sync(["claude"])
        _, second = self.sync(["claude"])

        self.assertIn("removed .codex/agents/", first)
        self.assertFalse((self.root / ".codex").exists())
        self.assertEqual("", second)

    def test_sync_accepts_repeated_tools_and_regenerates_a_removed_tool(self):
        self.sync(["copilot"])

        self.sync(["copilot", "claude"])

        self.assertEqual(["claude", "copilot"], self.existing_tools())

    def test_sync_keeps_unmarked_files_of_unselected_tools(self):
        write(self.root, ".claude/agents/mine.md", "---\nname: mine\n---\n")
        write(self.root, ".codex/agents/mine.toml", 'name = "mine"\n')

        self.sync(["copilot"])

        self.assertTrue((self.root / ".claude/agents/mine.md").is_file())
        self.assertTrue((self.root / ".codex/agents/mine.toml").is_file())

    def test_sync_rejects_unknown_tool_and_writes_nothing(self):
        before = (self.root / ".claude/agents/code-reviewer.md").read_text(encoding="utf-8")

        code, output = self.sync(["copilot", "cursor"])

        self.assertEqual(1, code)
        self.assertIn("::error::", output)
        self.assertIn("cursor", output)
        self.assertEqual(["claude", "codex", "copilot"], self.existing_tools())
        self.assertEqual(
            before, (self.root / ".claude/agents/code-reviewer.md").read_text(encoding="utf-8")
        )

    def test_sync_reads_tools_from_git_config(self):
        self.git("init")
        self.git("config", "agent-setup.tools", " copilot , claude ")

        self.sync()

        self.assertEqual(["claude", "copilot"], self.existing_tools())

    def test_tool_flag_overrides_git_config(self):
        self.git("init")
        self.git("config", "agent-setup.tools", "copilot")

        self.sync(["codex"])

        self.assertEqual(["codex"], self.existing_tools())

    def test_sync_rejects_unknown_tool_in_git_config_and_writes_nothing(self):
        self.git("init")
        self.git("config", "agent-setup.tools", "copilot,cursor")

        code, output = self.sync()

        self.assertEqual(1, code)
        self.assertIn("::error::", output)
        self.assertIn("cursor", output)
        self.assertEqual(["claude", "codex", "copilot"], self.existing_tools())

    def test_sync_generates_all_tools_when_git_config_is_unset(self):
        self.git("init")
        self.sync(["copilot"])

        self.sync()

        self.assertEqual(["claude", "codex", "copilot"], self.existing_tools())

    def test_sync_hints_at_setup_when_no_selection_exists(self):
        self.git("init")
        (self.root / ".claude/agents/code-reviewer.md").unlink()

        code, output = self.sync()

        self.assertEqual(0, code)
        self.assertIn(
            "Generated sub-agents for all tools; run npm run setup-agents to choose yours.",
            output,
        )

    def test_sync_does_not_hint_when_a_selection_exists(self):
        self.git("init")
        self.git("config", "agent-setup.tools", "copilot")

        _, configured = self.sync()
        _, flagged = self.sync(["claude"])

        self.assertNotIn("setup-agents", configured)
        self.assertNotIn("setup-agents", flagged)

    def test_sync_stays_quiet_when_nothing_changed(self):
        self.git("init")

        _, output = self.sync()

        self.assertEqual("", output)

    def setup_run(self, answers, isatty=True):
        replies = iter(answers)
        prompts = []

        def ask(prompt):
            prompts.append(prompt)
            return next(replies)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = setup.run_setup(self.root, ask, lambda: isatty)
        return code, output.getvalue(), prompts

    def configured(self):
        result = subprocess.run(
            ["git", "config", "--get", "agent-setup.tools"],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def test_setup_refuses_without_a_terminal(self):
        self.git("init")

        code, output, prompts = self.setup_run([], isatty=False)

        self.assertEqual(1, code)
        self.assertEqual([], prompts)
        self.assertIn("git config agent-setup.tools", output)
        self.assertIn("sync", output)
        self.assertIsNone(self.configured())

    def test_setup_saves_choice_in_tool_order_and_syncs_only_those_tools(self):
        self.git("init")

        code, output, prompts = self.setup_run(["3, 1"])

        self.assertEqual(0, code)
        self.assertEqual("copilot,codex", self.configured())
        self.assertIn("Saved: git config agent-setup.tools copilot,codex", output)
        self.assertEqual(["codex", "copilot"], self.existing_tools())
        self.assertIn("Which coding agents do you use? (comma-separated numbers)", output + "".join(prompts))
        self.assertIn("1) GitHub Copilot", output)
        self.assertIn("2) Claude Code", output)
        self.assertIn("3) Codex", output)
        self.assertNotIn("setup-agents", output)

    def test_setup_accepts_names_and_spaces(self):
        self.git("init")

        self.setup_run(["Claude codex"])

        self.assertEqual("claude,codex", self.configured())

    def test_setup_asks_again_on_invalid_input(self):
        self.git("init")

        code, output, prompts = self.setup_run(["9", "cursor", "2"])

        self.assertEqual(0, code)
        self.assertEqual(3, len(prompts))
        self.assertEqual("claude", self.configured())

    def test_setup_asks_again_on_non_ascii_digits(self):
        self.git("init")

        code, output, prompts = self.setup_run(["\u00b2", "\u0663", "2"])

        self.assertEqual(0, code)
        self.assertEqual(3, len(prompts))
        self.assertEqual("claude", self.configured())

    def test_setup_cancel_saves_nothing_and_exits_1(self):
        self.git("init")
        for error in (EOFError, KeyboardInterrupt):
            with self.subTest(error.__name__):
                output = io.StringIO()

                def ask(prompt, error=error):
                    raise error()

                with contextlib.redirect_stdout(output):
                    code = setup.run_setup(self.root, ask, lambda: True)

                self.assertEqual(1, code)
                self.assertIn("\nCancelled; nothing changed.", output.getvalue())
                self.assertIsNone(self.configured())
                self.assertEqual(["claude", "codex", "copilot"], self.existing_tools())

    def test_setup_prompt_describes_current_choice_and_enter(self):
        self.git("init")

        _, output, prompts = self.setup_run(["1"])

        self.assertIn("Current choice: none (all tools are generated)", output)
        self.assertEqual(["Choice [Enter keeps current]: "], prompts)

    def test_setup_prompt_shows_configured_tools(self):
        self.git("init")
        self.git("config", "agent-setup.tools", "codex,claude")

        _, output, _ = self.setup_run([""])

        self.assertIn("Current choice: claude, codex\n", output)

    def test_setup_enter_keeps_current_choice(self):
        self.git("init")
        self.git("config", "agent-setup.tools", "codex")

        code, output, prompts = self.setup_run([""])

        self.assertEqual(0, code)
        self.assertEqual("codex", self.configured())
        self.assertEqual(["codex"], self.existing_tools())
        self.assertIn("codex", "".join(prompts) + output)

    def test_setup_enter_without_current_choice_selects_all(self):
        self.git("init")

        self.setup_run([""])

        self.assertEqual("claude,copilot,codex", self.configured())
        self.assertEqual(["claude", "codex", "copilot"], self.existing_tools())

    def test_check_does_not_depend_on_selected_tools(self):
        self.sync(["copilot"])

        self.assertEqual([], self.problems())

    def test_check_flags_tracked_generated_files(self):
        self.git("init")
        self.git("add", "-f", *self.generated_paths())

        problems = self.problems()

        self.assertEqual(3, len(problems), problems)
        for path in self.generated_paths():
            self.assertTrue(any(problem.startswith(f"{path}:") for problem in problems), path)
        self.assertIn("git rm --cached", problems[0])

    def test_check_ignores_tracked_source_files_and_skills(self):
        self.git("init")
        self.git("add", "-f", "agent-definitions", ".claude/skills")

        self.assertEqual([], self.problems())

    def test_check_flags_tracked_hand_written_file_in_a_tool_folder(self):
        self.git("init")
        write(self.root, ".claude/agents/mine.md", "---\nname: mine\n---\n")
        self.git("add", "-f", ".claude/agents/mine.md")

        problems = self.problems()

        self.assertEqual(1, len(problems), problems)
        self.assertTrue(problems[0].startswith(".claude/agents/mine.md:"), problems)
        self.assertIn("not versioned", problems[0])
        self.assertIn("agent-definitions/", problems[0])
        self.assertIn("git rm --cached .claude/agents/mine.md", problems[0])
        self.assertNotIn("generated files", problems[0])

    def test_check_flags_any_tracked_file_under_the_tool_folders(self):
        self.git("init")
        paths = [".github/agents/notes.txt", ".claude/agents/sub/deep.md", ".codex/agents/mine.md"]
        for path in paths:
            write(self.root, path, "x\n")
        self.git("add", "-f", *paths)

        problems = self.problems()

        self.assertEqual(3, len(problems), problems)
        for path in paths:
            self.assertTrue(any(problem.startswith(f"{path}:") for problem in problems), path)

    def test_check_reports_git_failures_instead_of_skipping_the_tracked_files_check(self):
        self.git("init")
        failures = {
            "git missing": FileNotFoundError("git"),
            "dubious ownership": subprocess.CompletedProcess(
                [], 128, "", "fatal: detected dubious ownership in repository"
            ),
        }
        for name, failure in failures.items():
            with self.subTest(name):
                if isinstance(failure, Exception):
                    patch = mock.patch.object(setup.subprocess, "run", side_effect=failure)
                else:
                    patch = mock.patch.object(setup.subprocess, "run", return_value=failure)
                with patch:
                    problems = self.problems()

                self.assertEqual(1, len(problems), problems)
                self.assertIn("could not check Git state", problems[0])

    LEFTHOOK_HOOK = '#!/bin/sh\ncall_lefthook() { lefthook run "post-checkout" "$@"; }\n'
    REMOVE_COMMAND = (
        "rm -f .git/hooks/post-checkout .git/hooks/post-merge .git/hooks/post-rewrite"
    )

    def write_hook(self, name, text=None, directory=".git/hooks"):
        path = self.root / directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.LEFTHOOK_HOOK if text is None else text, encoding="utf-8")
        path.chmod(0o755)

    def test_check_flags_leftover_lefthook_post_hooks_and_prints_the_removal(self):
        self.git("init")
        for name in ("post-checkout", "post-merge", "post-rewrite"):
            self.write_hook(name)

        code, output = self.check_output()

        self.assertEqual(1, code)
        self.assertIn("::error::", output)
        self.assertIn(self.REMOVE_COMMAND, output)
        self.assertNotIn(self.HINT, output)

    def test_check_flags_a_single_leftover_hook(self):
        self.git("init")
        self.write_hook("post-merge")

        problems = self.problems()

        self.assertEqual(1, len(problems), problems)
        self.assertIn("post-merge", problems[0])
        self.assertIn("lefthook", problems[0])

    def test_check_ignores_other_tools_hooks_and_the_hooks_lefthook_still_installs(self):
        self.git("init")
        self.write_hook("post-checkout", "#!/bin/sh\ngit lfs post-checkout \"$@\"\n")
        self.write_hook("pre-commit")
        self.write_hook("pre-push")

        self.assertEqual([], self.problems())

    def test_check_passes_without_hooks_as_in_ci(self):
        self.git("init")
        code, _ = self.check_output()
        self.assertEqual(0, code)

    def test_check_respects_core_hooks_path(self):
        self.git("init")
        self.git("config", "core.hooksPath", ".githooks")
        self.write_hook("post-checkout")  # in .git/hooks, which Git no longer runs
        self.assertEqual([], self.problems())

        self.write_hook("post-checkout", directory=".githooks")

        problems = self.problems()
        self.assertEqual(1, len(problems), problems)
        self.assertIn("rm -f .githooks/post-checkout", problems[0])

    def check_output(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = setup.run_check(self.root)
        return code, output.getvalue()

    HINT = "Restore generated Spec Kit files"

    def test_check_prints_restore_hint_only_for_spec_kit_and_source_problems(self):
        self.git("init")
        self.git("add", "-f", ".claude/agents/code-reviewer.md")
        code, output = self.check_output()
        self.assertEqual(1, code)
        self.assertIn("::error::", output)
        self.assertNotIn(self.HINT, output)

        self.write_manifest(version="1.3.0")
        code, output = self.check_output()
        self.assertEqual(1, code)
        self.assertIn(self.HINT, output)

    def test_check_skips_tracked_files_check_outside_a_git_repository(self):
        self.assertEqual([], self.problems())

    def test_check_does_not_report_tracked_claude_agents_symlink_twice(self):
        self.git("init")
        shutil.rmtree(self.root / ".claude/agents")
        os.symlink("../agent-definitions", self.root / ".claude/agents")
        self.git("add", "-f", ".claude/agents")

        problems = self.problems()

        self.assertEqual(1, len(problems), problems)
        self.assertIn("is a symlink", problems[0])

    def test_stale_generated_file_passes_check_and_sync_regenerates_it(self):
        for path in (
            ".github/agents/code-reviewer.agent.md",
            ".claude/agents/code-reviewer.md",
            ".codex/agents/code-reviewer.toml",
        ):
            with self.subTest(path=path):
                file = self.root / path
                original = file.read_text(encoding="utf-8")
                file.write_text(original + "edited\n", encoding="utf-8")

                self.assertEqual([], self.problems())
                self.sync()
                self.assertEqual(original, file.read_text(encoding="utf-8"))

    def test_sync_recreates_missing_generated_file_in_each_folder(self):
        for path in (
            ".github/agents/code-reviewer.agent.md",
            ".claude/agents/code-reviewer.md",
            ".codex/agents/code-reviewer.toml",
        ):
            with self.subTest(path=path):
                file = self.root / path
                original = file.read_text(encoding="utf-8")
                file.unlink()

                self.assertEqual([], self.problems())
                self.sync()
                self.assertEqual(original, file.read_text(encoding="utf-8"))

    def test_orphaned_generated_files_pass_check_and_sync_removes_them(self):
        orphans = (
            ".github/agents/old.agent.md",
            ".claude/agents/old.md",
            ".codex/agents/old.toml",
        )
        for path in orphans:
            write(self.root, path, generated_content(path))

        self.assertEqual([], self.problems())
        self.sync()
        for path in orphans:
            self.assertFalse((self.root / path).exists())
        self.assertEqual([], self.problems())

    def test_directories_named_like_agent_files_do_not_crash_check_or_sync(self):
        for path in (
            ".github/agents/dir.agent.md",
            ".github/agents/olddir.md",
            ".claude/agents/dir.md",
            ".codex/agents/dir.toml",
        ):
            (self.root / path).mkdir()

        self.assertEqual([], self.problems())
        code, _ = self.sync()
        self.assertEqual(0, code)
        self.assertTrue((self.root / ".claude/agents/dir.md").is_dir())

    def test_unmarked_files_survive_sync_and_pass_check(self):
        for path in (
            ".github/agents/mine.agent.md",
            ".claude/agents/mine.md",
            ".codex/agents/mine.toml",
        ):
            with self.subTest(path=path):
                write(self.root, path, "---\nname: mine\n---\nhand-written\n")

                self.assertEqual([], self.problems())
                self.sync()
                self.assertEqual("---\nname: mine\n---\nhand-written\n",
                                 (self.root / path).read_text(encoding="utf-8"))
                self.assertEqual([], self.problems())

    def test_flags_old_layout_agent_and_sync_keeps_it(self):
        write(self.root, ".github/agents/code-reviewer.md", AGENT)

        self.assert_single_problem(
            ".github/agents/code-reviewer.md: move it to agent-definitions/"
        )
        code, output = self.sync()

        self.assertEqual(1, code)
        self.assertIn("::error::.github/agents/code-reviewer.md", output)
        self.assertTrue((self.root / ".github/agents/code-reviewer.md").exists())

    def test_flags_claude_agents_symlink(self):
        shutil.rmtree(self.root / ".claude/agents")
        os.symlink("../.github/agents", self.root / ".claude/agents")

        target = self.root / ".github/agents"
        before = {f.name: f.read_bytes() for f in target.iterdir()}

        self.assert_single_problem(".claude/agents: is a symlink")
        self.assertEqual(1, self.sync()[0])
        self.assertEqual(before, {f.name: f.read_bytes() for f in target.iterdir()})

    def test_flags_claude_agents_that_is_a_file(self):
        shutil.rmtree(self.root / ".claude/agents")
        write(self.root, ".claude/agents", "x")

        self.assert_single_problem(".claude/agents: is not a directory")

    def test_sync_is_idempotent(self):
        code, output = self.sync()

        self.assertEqual(0, code)
        self.assertEqual("", output)

    def test_ignores_nested_frontmatter_keys(self):
        write(self.root, SOURCE, AGENT.replace("tools: [Read, Grep]", "metadata:\n  description: nested value"))
        self.sync()

        pointer = (self.root / ".codex/agents/code-reviewer.toml").read_text(encoding="utf-8")
        claude = (self.root / ".claude/agents/code-reviewer.md").read_text(encoding="utf-8")

        self.assertIn('description = "Reviews \\"diffs\\" before merge."', pointer)
        self.assertIn("metadata:\n  description: nested value\nmodel: opus\n", claude)

    def test_flags_agent_without_description(self):
        write(self.root, "agent-definitions/bad.md", "---\nname: bad\n---\n")

        self.assertIn(
            "agent-definitions/bad.md: frontmatter needs 'description'", self.problems()
        )

    def test_flags_agent_without_frontmatter_instead_of_crashing(self):
        write(self.root, "agent-definitions/README.md", "# Agents\n")

        self.assertIn("agent-definitions/README.md: missing frontmatter", self.problems())

    def test_flags_quoted_description(self):
        write(self.root, SOURCE, AGENT.replace('Reviews "diffs" before merge.', '"Reviews"'))

        self.assert_single_problem("write 'description' as a plain one-line value")

    def test_flags_description_wrapped_over_two_lines(self):
        write(self.root, SOURCE, AGENT.replace("before merge.", "before\n  merge."))

        self.assert_single_problem("write 'description' as a plain one-line value")

    def test_flags_description_that_starts_on_the_next_line(self):
        wrapped = AGENT.replace('description: Reviews "diffs"', 'description:\n  Reviews "diffs"')
        write(self.root, SOURCE, wrapped)

        self.assert_single_problem("write 'description' as a plain one-line value")

    def test_allows_wrapped_tools_which_are_copied_verbatim(self):
        write(self.root, SOURCE, AGENT.replace("tools: [Read, Grep]", "tools: [Read,\n  Grep]"))
        self.sync()

        claude = (self.root / ".claude/agents/code-reviewer.md").read_text(encoding="utf-8")

        self.assertIn("tools: [Read,\n  Grep]\nmodel: opus\n", claude)
        self.assertEqual([], self.problems())

    def test_flags_model_and_effort_keys_in_source(self):
        for key in ("model", "effort", "reasoning-effort"):
            with self.subTest(key=key):
                write(self.root, SOURCE, AGENT.replace("tools:", f"{key}: high\ntools:"))

                self.assert_single_problem(
                    f"{SOURCE}: remove '{key}'; set it in agent-definitions/agents.toml"
                )

    def test_flags_name_that_differs_from_file_name(self):
        write(self.root, "agent-definitions/reviewer.md", AGENT)

        self.assertIn(
            "agent-definitions/reviewer.md: 'name' must match the file name (reviewer)",
            self.problems(),
        )

    def test_sync_refuses_invalid_agent_and_writes_nothing(self):
        write(self.root, "agent-definitions/bad.md", "---\nname: bad\n---\n")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            self.assertEqual(1, setup.run_sync(self.root))

        self.assertIn("::error::agent-definitions/bad.md", output.getvalue())
        self.assertFalse((self.root / ".codex/agents/bad.toml").exists())

    def set_config(self, content):
        write(self.root, CONFIG_PATH, content)

    def assert_config_problem(self, fragment):
        problems = self.problems()
        self.assertTrue(any(fragment in p and p.startswith(CONFIG_PATH) for p in problems), problems)

    def test_flags_missing_config(self):
        (self.root / CONFIG_PATH).unlink()

        self.assert_config_problem("missing")

    def test_flags_invalid_toml(self):
        self.set_config("[agents\n")

        self.assert_config_problem("invalid TOML")

    def test_flags_source_agent_without_entry(self):
        self.set_config(CONFIG.replace('code-reviewer = "review"', ""))

        self.assert_config_problem("[agents] has no entry for 'code-reviewer'")

    def test_flags_entry_without_source_agent(self):
        self.set_config(CONFIG.replace("[agents]\n", '[agents]\nghost = "review"\n'))

        self.assert_config_problem("[agents] 'ghost' has no agent-definitions/ghost.md")

    def test_flags_unknown_profile(self):
        self.set_config(CONFIG.replace('"review"', '"nope"', 1))

        self.assert_config_problem("[agents] 'code-reviewer' uses unknown profile 'nope'")

    def test_flags_unused_profile(self):
        self.set_config(CONFIG + "[profiles.spare.claude]\nmodel = \"x\"\n")

        self.assert_config_problem("profile 'spare' is not used")

    def test_flags_unknown_tool_and_setting_keys(self):
        self.set_config(CONFIG + "[profiles.review.cursor]\nmodel = \"x\"\n")
        self.assert_config_problem("profile 'review' has unknown tool 'cursor'")

        self.set_config(CONFIG.replace('effort = "medium"', 'speed = "fast"'))
        self.assert_config_problem("profile 'review' claude has unknown key 'speed'")

    def test_flags_effort_level_not_shared_by_all_tools(self):
        self.set_config(CONFIG.replace('effort = "medium"', 'effort = "ultra"'))

        self.assert_config_problem("claude effort must be one of low, medium, high, xhigh, max")

    def test_flags_model_that_is_not_a_plain_one_line_string(self):
        for model in ('""', '"a: b"', '"a\\nb"', "5"):
            with self.subTest(model=model):
                self.set_config(CONFIG.replace('model = "opus"', f"model = {model}"))

                self.assert_config_problem("claude model must be a non-empty plain one-line string")

    def test_flags_config_type_errors_instead_of_crashing(self):
        cases = {
            "agents not a table": ('agents = "x"\n', "[agents] and [profiles] must be tables"),
            "profiles not a table": (
                'profiles = "x"\n[agents]\ncode-reviewer = "review"\n',
                "must be tables",
            ),
            "profile not a table": (
                '[agents]\ncode-reviewer = "review"\n[profiles]\nreview = "x"\n',
                "profile 'review' must be a table of tools",
            ),
            "tool not a table": (
                '[agents]\ncode-reviewer = "review"\n[profiles.review]\nclaude = "opus"\n',
                "profile 'review' claude must be a table",
            ),
            "effort not a string": (
                CONFIG.replace('effort = "medium"', "effort = 5"),
                "claude effort must be one of",
            ),
            "effort is a list": (
                CONFIG.replace('effort = "medium"', 'effort = ["low"]'),
                "claude effort must be one of",
            ),
            "profile name not a string": (
                CONFIG.replace('code-reviewer = "review"', 'code-reviewer = ["x"]'),
                "[agents] 'code-reviewer' must name a profile (string)",
            ),
        }
        for label, (content, fragment) in cases.items():
            with self.subTest(label):
                self.set_config(content)

                self.assert_config_problem(fragment)

    def test_sync_refuses_invalid_config_and_writes_nothing(self):
        self.set_config("[agents\n")
        before = (self.root / ".claude/agents/code-reviewer.md").read_text(encoding="utf-8")
        code, output = self.sync()

        self.assertEqual(1, code)
        self.assertIn(f"::error::{CONFIG_PATH}", output)
        self.assertEqual(
            before, (self.root / ".claude/agents/code-reviewer.md").read_text(encoding="utf-8")
        )


if __name__ == "__main__":
    unittest.main()
