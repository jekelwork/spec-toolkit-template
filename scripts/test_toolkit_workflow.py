from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/toolkit.yml"
OLD_EXPORT_STEP_NAME = "Check the toolkit export manifest classifies every tracked file"


ROADMAP_STEP_NAME = "Check the roadmap format once the repository is initialized"


def step_script(name, workflow=WORKFLOW):
    """Return the `run` script of a step (plain text extraction, no YAML parser).

    Works for workflow steps (`      - name:`) and composite-action steps (`    - name:`).
    """
    text = workflow.read_text(encoding="utf-8")
    start = re.search(rf"^( *)- name: {re.escape(name)}\n", text, re.MULTILINE)
    if not start:
        raise AssertionError(f"step {name!r} not found in {workflow.name}")
    indent = len(start.group(1))
    following = re.search(rf"^ {{{indent}}}- ", text[start.end() :], re.MULTILINE)
    body = text[start.end() : start.end() + following.start()] if following else text[start.end() :]
    match = re.search(
        rf"^ {{{indent + 2}}}run: \|\n((?:(?: {{{indent + 4}}}.*)?\n)+)", body, re.MULTILINE
    )
    if not match:
        raise AssertionError(f"step {name!r} has no run block in {workflow.name}")
    return "\n".join(line[indent + 4 :] for line in match.group(1).splitlines())


class SourceOnlyExportToolingTest(unittest.TestCase):
    """The export tooling is source-only: the published workflow must not call it."""

    def test_the_published_workflow_does_not_call_the_export_tooling(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn(OLD_EXPORT_STEP_NAME, text)
        self.assertNotIn("export_toolkit.py", text)
        self.assertNotIn("--toolkit-export-source", text)


BOARD_ACTION = WORKFLOW.parent.parent / "actions/board-token/action.yml"
BOARD_ACTION_USE = "uses: ./.github/actions/board-token"
TOKEN_STEP = "Create optional board token"


def action_text():
    return BOARD_ACTION.read_text(encoding="utf-8")


def action_step_block(name):
    return action_text().split(f"- name: {name}\n")[1].split("\n    - ")[0]


def action_step_condition(name):
    return re.search(r"^      if: (.*)$", action_step_block(name), re.MULTILINE).group(1)


class AppTokenRepositoryScopeTest(unittest.TestCase):
    """A schedule payload has no `repository`, so an App token must not be scoped through it."""

    def test_repositories_never_depends_on_the_event_repository(self):
        block = action_step_block(TOKEN_STEP)
        expression = re.search(r"^        repositories: (.*)$", block, re.MULTILINE).group(1)
        self.assertNotIn("github.event.repository", expression)
        # Non-empty even when the event has no repository.
        self.assertEqual(evaluate(expression, {"steps.repo.outputs.name": "example-repo"}), "example-repo")

    def test_the_repository_name_step_runs_first_and_strips_the_owner(self):
        text = action_text()
        self.assertLess(text.index("id: repo\n"), text.index(f"- name: {TOKEN_STEP}\n"))
        script = step_script("Read the repository name", BOARD_ACTION)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            subprocess.run(
                ["bash", "-e", "-c", script],
                env={
                    "PATH": "/usr/bin:/bin",
                    "GITHUB_REPOSITORY": "example-org/example-repo",
                    "GITHUB_OUTPUT": str(output),
                },
                check=True,
            )
            self.assertEqual(output.read_text(), "name=example-repo\n")


class BoardActionUseTest(unittest.TestCase):
    """The three board workflows share one local composite action, run after the trusted checkout."""

    def test_every_board_workflow_uses_the_action_after_its_checkout(self):
        for workflow in BOARD_WORKFLOWS:
            with self.subTest(workflow=workflow):
                text = workflow_text(workflow)
                self.assertEqual(text.count(BOARD_ACTION_USE), 1)
                self.assertLess(text.index("actions/checkout@"), text.index(BOARD_ACTION_USE))
                step = step_block(workflow, BOARD_STEP)
                self.assertIn("id: board\n", step)
                self.assertIn("client-id: ${{ secrets.PROJECT_STATUS_APP_ID }}", step)
                self.assertIn("private-key: ${{ secrets.PROJECT_BOARD_APP_PRIVATE_KEY }}", step)

    def test_the_workflows_no_longer_repeat_the_shared_steps(self):
        for workflow in BOARD_WORKFLOWS:
            with self.subTest(workflow=workflow):
                text = workflow_text(workflow)
                for name in (OWNER_STEP, "Read the repository name", TOKEN_STEP):
                    self.assertNotIn(f"- name: {name}\n", text)
                self.assertNotIn("create-github-app-token", text)

    def test_the_action_declares_the_owner_and_token_outputs_and_secret_inputs(self):
        text = action_text()
        self.assertIn("using: composite", text)
        self.assertIn("value: ${{ steps.project-config.outputs.owner }}", text)
        self.assertIn("value: ${{ steps.app-token.outputs.token }}", text)
        for name in ("client-id", "private-key", "has-board-pat"):
            self.assertRegex(text, rf"(?m)^  {name}:\n")
        self.assertEqual(secret_interpolations_in_run(text), [])
        self.assertNotIn("${{ secrets.", text)

    def test_every_run_step_in_the_action_declares_shell_bash(self):
        steps = re.split(r"(?m)^    - ", action_text().split("\nruns:\n")[1])[1:]
        run_steps = [step for step in steps if re.search(r"(?m)^      run:", step)]
        self.assertTrue(run_steps)
        for step in run_steps:
            with self.subTest(step=step.splitlines()[0]):
                self.assertRegex(step, r"(?m)^      shell: bash$")


class ToolkitWorkflowRoadmapStepTest(unittest.TestCase):
    def run_step(self, state_script, roadmap_exit=0):
        """Run the step under `bash -e` with stubbed project_context.py and check_roadmap.py."""
        with tempfile.TemporaryDirectory() as directory:
            scripts = Path(directory) / "scripts"
            scripts.mkdir()
            (scripts / "project_context.py").write_text(state_script, encoding="utf-8")
            (scripts / "check_roadmap.py").write_text(
                f'import sys\nprint("ROADMAP CHECK RAN")\nsys.exit({roadmap_exit})\n',
                encoding="utf-8",
            )
            return subprocess.run(
                ["bash", "-e", "-c", step_script(ROADMAP_STEP_NAME)],
                cwd=directory,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_the_check_runs_once_initialized(self):
        result = self.run_step('print("initialized")\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ROADMAP CHECK RAN", result.stdout)
        self.assertNotIn("::notice::", result.stdout)

    def test_a_failing_check_fails_the_step_once_initialized(self):
        result = self.run_step('print("initialized")\n', roadmap_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ROADMAP CHECK RAN", result.stdout)

    def test_the_check_is_skipped_with_a_notice_before_bootstrap(self):
        result = self.run_step('print("uninitialized")\n', roadmap_exit=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ROADMAP CHECK RAN", result.stdout)
        self.assertIn("::notice::", result.stdout)
        self.assertIn("docs/bootstrap.md", result.stdout)

    def test_a_failing_state_query_fails_the_step(self):
        result = self.run_step('import sys\nsys.exit(1)\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("ROADMAP CHECK RAN", result.stdout)
        self.assertNotIn("::notice::", result.stdout)

    def test_unexpected_output_fails_the_step(self):
        for output in ("INITIALIZED", "initialized ", "noise\ninitialized", "", "yes"):
            with self.subTest(output=output):
                result = self.run_step(f"print({output!r})\n")
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("::error::", result.stdout)
                self.assertNotIn("ROADMAP CHECK RAN", result.stdout)


CONTEXT_STEP_NAME = "Validate the project context once the repository is initialized"


class ToolkitWorkflowContextStepTest(unittest.TestCase):
    def run_step(self, state, context_exit=0):
        """Run the step under `bash -e` with a stubbed project_context.py."""
        stub = (
            "import sys\n"
            f'if "--bootstrap-state" in sys.argv:\n    print({state!r})\n    sys.exit(0)\n'
            f'print("CONTEXT VALIDATED")\n'
            f'print("STUB CONTEXT ERROR", file=sys.stderr)\nsys.exit({context_exit})\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            scripts = Path(directory) / "scripts"
            scripts.mkdir()
            (scripts / "project_context.py").write_text(stub, encoding="utf-8")
            return subprocess.run(
                ["bash", "-e", "-c", step_script(CONTEXT_STEP_NAME)],
                cwd=directory, capture_output=True, text=True, check=False,
            )

    def test_the_context_is_validated_once_initialized(self):
        result = self.run_step("initialized")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("CONTEXT VALIDATED", result.stdout)

    def test_an_invalid_context_fails_the_step_once_initialized(self):
        result = self.run_step("initialized", context_exit=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("STUB CONTEXT ERROR", result.stderr)
        self.assertNotIn("CONTEXT VALIDATED", result.stdout)

    def test_the_step_is_skipped_in_the_uninitialized_template(self):
        result = self.run_step("uninitialized", context_exit=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("STUB CONTEXT ERROR", result.stderr)
        self.assertIn("::notice::", result.stdout)

    def test_unexpected_state_output_fails_the_step(self):
        result = self.run_step("INITIALIZED")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("::error::", result.stdout)
        self.assertNotIn("CONTEXT VALIDATED", result.stdout)


TEMPLATE_STEP_NAME = "Keep the template uninitialized"


class TemplateStaysUninitializedStepTest(unittest.TestCase):
    """The template repository must never ship project files; repositories created from it skip this."""

    def run_step(self, state_script):
        with tempfile.TemporaryDirectory() as directory:
            scripts = Path(directory) / "scripts"
            scripts.mkdir()
            (scripts / "project_context.py").write_text(state_script, encoding="utf-8")
            return subprocess.run(
                ["bash", "-e", "-c", step_script(TEMPLATE_STEP_NAME)],
                cwd=directory,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_it_runs_only_in_a_repository_marked_as_a_template(self):
        condition = step_condition("toolkit.yml", TEMPLATE_STEP_NAME)
        self.assertEqual(condition, "github.event.repository.is_template")
        self.assertTrue(evaluate(condition, {"github.event.repository.is_template": True}))
        self.assertFalse(evaluate(condition, {"github.event.repository.is_template": False}))

    def test_it_runs_before_the_roadmap_check(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertLess(
            text.index(f"- name: {TEMPLATE_STEP_NAME}\n"),
            text.index(f"- name: {ROADMAP_STEP_NAME}\n"),
        )

    def test_an_uninitialized_template_passes(self):
        result = self.run_step('print("uninitialized")\n')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("::error::", result.stdout)

    def test_an_initialized_template_fails_and_names_the_files(self):
        result = self.run_step('print("initialized")\n')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("::error::", result.stdout)
        self.assertIn("must stay uninitialized", result.stdout)
        for name in ("project-context.toml", "ROADMAP.md", "constitution", "specs/"):
            self.assertIn(name, result.stdout)

    def test_a_failing_state_query_fails_the_step(self):
        result = self.run_step("import sys\nsys.exit(1)\n")
        self.assertNotEqual(result.returncode, 0)

    def test_unexpected_output_fails_the_step(self):
        for output in ("UNINITIALIZED", "uninitialized ", "noise\nuninitialized", "", "yes"):
            with self.subTest(output=output):
                result = self.run_step(f"print({output!r})\n")
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("::error::", result.stdout)


WORKFLOWS = WORKFLOW.parent
OWNER_STEP = "Read configured Project owner"
BOARD_STEP = "Set up the board credential"
ISSUE_SYNCS = ("sync-roadmap-issues.yml", "sync-phase-issues.yml")
BOARD_WORKFLOWS = (*ISSUE_SYNCS, "sync-project-status.yml")


class SyncWorkflowHardeningTest(unittest.TestCase):
    def test_issue_syncs_keep_no_git_credentials(self):
        for workflow in ISSUE_SYNCS:
            with self.subTest(workflow=workflow):
                self.assertIn("persist-credentials: false", workflow_text(workflow))

    def test_the_roadmap_sync_follows_the_default_branch(self):
        text = workflow_text("sync-roadmap-issues.yml")
        self.assertNotIn("branches: [main]", text)
        self.assertNotIn("ref: main", text)
        self.assertIn("persist-credentials: false", text)

    def roadmap_job_condition(self):
        job = workflow_text("sync-roadmap-issues.yml").split("jobs:\n")[1]
        return re.search(r"^    if: (.*)$", job, re.MULTILINE).group(1)

    def test_the_roadmap_sync_condition_per_event(self):
        condition = self.roadmap_job_condition()
        cases = (
            # (event, ref, default branch as the payload reports it) -> runs
            ("push", "refs/heads/main", "main", True),
            ("push", "refs/heads/feature", "main", False),
            ("workflow_dispatch", "refs/heads/main", "main", True),
            ("workflow_dispatch", "refs/heads/feature", "main", False),
            # A schedule payload has no `repository`, so the default branch is null.
            ("schedule", "refs/heads/main", "", True),
        )
        for event, ref, default, runs in cases:
            with self.subTest(event=event, ref=ref):
                context = {
                    "github.event_name": event,
                    "github.ref": ref,
                    "github.event.repository.default_branch": default,
                }
                self.assertEqual(bool(evaluate(condition, context)), runs)

    def test_the_old_roadmap_condition_never_ran_on_schedule(self):
        old = "github.ref == format('refs/heads/{0}', github.event.repository.default_branch)"
        context = {
            "github.event_name": "schedule",
            "github.ref": "refs/heads/main",
            "github.event.repository.default_branch": "",
        }
        self.assertFalse(evaluate(old, context))

    def test_the_roadmap_checkout_falls_back_to_the_event_ref(self):
        text = workflow_text("sync-roadmap-issues.yml")
        expression = re.search(r"^          ref: (.*)$", text, re.MULTILINE).group(1)
        self.assertEqual(
            evaluate(expression, {"github.event.repository.default_branch": "main", "github.ref": "refs/heads/main"}),
            "main",
        )
        self.assertEqual(
            evaluate(expression, {"github.event.repository.default_branch": "", "github.ref": "refs/heads/main"}),
            "refs/heads/main",
        )

    def test_the_phase_sync_schedule_runs_on_the_default_branch(self):
        text = workflow_text("sync-phase-issues.yml")
        condition = re.search(r"^    if: >-\n((?:      .*\n)+)", text, re.MULTILINE).group(1)
        context = {
            "github.event_name": "schedule",
            "github.ref": "refs/heads/main",
            "github.event.repository.default_branch": "",
            "github.event.pull_request.merged": "",
            "github.event.pull_request.base.ref": "",
        }
        self.assertTrue(evaluate(" ".join(condition.split()), context))

    def test_issue_mutating_syncs_cannot_be_cancelled_by_skipped_runs(self):
        # Workflow-level concurrency is taken before the job `if`, so a skipped feature-branch run
        # could replace a pending default-branch run; keep it on the job.
        for workflow in ("sync-roadmap-issues.yml", "sync-phase-issues.yml"):
            with self.subTest(workflow=workflow):
                text = workflow_text(workflow)
                self.assertNotRegex(text.split("jobs:\n")[0], r"(?m)^concurrency:")
                self.assertRegex(text.split("jobs:\n")[1], r"(?m)^    concurrency:")

    def test_the_feature_loop_does_not_word_split(self):
        text = workflow_text("sync-phase-issues.yml")
        self.assertNotIn("for feature in $features", text)
        self.assertIn("while IFS= read -r feature", text)


class ProjectStatusTriggerTest(unittest.TestCase):
    """Trigger and job condition of sync-project-status.yml (see the comment at the top of the workflow)."""

    def condition(self):
        text = workflow_text("sync-project-status.yml").split("jobs:\n")[1]
        match = re.search(r"^    if: >-\n((?:      .*\n)+)", text, re.MULTILINE)
        self.assertIsNotNone(match)
        return " ".join(match.group(1).split())

    def runs(self, **overrides):
        context = {
            "github.event_name": "push",
            "github.ref": "refs/heads/1-phase-1-x",
            "github.event.repository.default_branch": "main",
        }
        context.update(overrides)
        return bool(evaluate(self.condition(), context))

    def test_create_trigger_is_absent(self):
        head = workflow_text("sync-project-status.yml").split("jobs:\n")[0]
        on_block = head.split("\non:\n")[1].split("\npermissions:")[0]
        self.assertIn("  push:\n", on_block)
        self.assertNotRegex(on_block, r"(?m)^  create:")

    def test_the_create_event_is_not_handled(self):
        text = workflow_text("sync-project-status.yml")
        self.assertNotIn("EVENT_REF", text)
        self.assertNotIn("ref_type", text)

    def test_a_phase_branch_push_runs(self):
        self.assertTrue(self.runs())

    def test_workflow_dispatch_runs_only_on_the_default_branch(self):
        self.assertTrue(self.runs(**{"github.event_name": "workflow_dispatch", "github.ref": "refs/heads/main"}))
        self.assertFalse(self.runs(**{"github.event_name": "workflow_dispatch", "github.ref": "refs/heads/feature"}))


class BoardOwnerStepTest(unittest.TestCase):
    """The shared action step; project_context.py decides skip, owner or failure."""

    def run_step(self, stub):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "scripts").mkdir()
            (root / "scripts" / "project_context.py").write_text(stub, encoding="utf-8")
            output_file = root / "github-output"
            output_file.write_text("", encoding="utf-8")
            result = subprocess.run(
                ["bash", "-e", "-c", step_script(OWNER_STEP, BOARD_ACTION)],
                cwd=directory,
                capture_output=True,
                text=True,
                check=False,
                env={
                    "PATH": "/usr/bin:/bin:" + str(Path(sys.executable).parent),
                    "GITHUB_OUTPUT": str(output_file),
                    "GITHUB_REPOSITORY": "example-org/repo",
                },
            )
            return result, output_file.read_text(encoding="utf-8")

    def test_the_owner_is_written_to_the_step_output(self):
        stub = "import sys\nprint(' '.join(sys.argv[1:]), file=sys.stderr)\nprint('example-org')\n"
        result, output = self.run_step(stub)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "owner=example-org\n")
        self.assertIn("--board-owner-or-skip", result.stderr)
        self.assertIn("--repository-owner example-org", result.stderr)

    def test_a_skip_leaves_the_owner_empty(self):
        result, output = self.run_step("pass\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output, "owner=\n")

    def test_a_failure_fails_the_step_without_an_owner(self):
        stub = "import sys\nprint('::error::Missing project context', file=sys.stderr)\nsys.exit(1)\n"
        result, output = self.run_step(stub)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("owner=", output)

    def test_the_step_always_runs_and_the_later_board_steps_wait_for_the_owner(self):
        self.assertNotIn("if:", action_step_block(OWNER_STEP))
        self.assertIn("steps.project-config.outputs.owner != ''", action_step_block(TOKEN_STEP))
        self.assertIn(
            "steps.board.outputs.owner != ''", step_block("sync-project-status.yml", "Reconcile statuses")
        )

    def test_the_duplicated_board_steps_are_gone(self):
        for workflow in BOARD_WORKFLOWS:
            with self.subTest(workflow=workflow):
                text = workflow_text(workflow)
                self.assertNotIn("Note the board credential", text)
                self.assertNotIn("Check that the Project board is configured", text)
                self.assertNotIn("--bootstrap-state", text)

    def test_the_skip_message_does_not_blame_only_the_app_secrets(self):
        for workflow in ISSUE_SYNCS:
            with self.subTest(workflow=workflow):
                text = workflow_text(workflow)
                self.assertIn("Board sync skipped (see notices above).", text)
                self.assertNotIn('echo "Board sync skipped: configure', text)




def evaluate(expression, context):
    """Evaluate the small GitHub expression subset used by the board gates.

    Supports `${{ }}`, ||, &&, ==, !=, parentheses, 'strings', true/false, context lookups and
    `format('...{0}', arg, ...)`. A property GitHub reports as null (a payload without it) is given
    as ''; any other unknown name is an error.
    `&&` and `||` return operands like GitHub does (first falsy / first truthy).
    """
    text = expression.strip()
    wrapped = re.fullmatch(r"\$\{\{(.*)\}\}", text, re.DOTALL)
    if wrapped:
        text = wrapped.group(1)
    token_pattern = re.compile(r"\s*('[^']*'|\|\||&&|==|!=|\(|\)|,|[\w.\-]+)")
    tokens = []
    end = 0
    while end < len(text.rstrip()):
        match = token_pattern.match(text, end)
        if not match:
            raise AssertionError(f"unsupported syntax at {text[end:].strip()!r} in {expression!r}")
        tokens.append(match.group(1))
        end = match.end()
    position = 0

    def peek():
        return tokens[position] if position < len(tokens) else None

    def take():
        nonlocal position
        position += 1
        return tokens[position - 1]

    def truthy(value):
        return value not in ("", False, None)

    def parse_or():
        value = parse_and()
        while peek() == "||":
            take()
            right = parse_and()
            value = value if truthy(value) else right
        return value

    def parse_and():
        value = parse_compare()
        while peek() == "&&":
            take()
            right = parse_compare()
            value = right if truthy(value) else value
        return value

    def parse_compare():
        value = parse_atom()
        while peek() in ("==", "!="):
            operator = take()
            right = parse_atom()
            same = str(value).lower() == str(right).lower()
            value = same if operator == "==" else not same
        return value

    def parse_atom():
        token = take()
        if token == "format" and peek() == "(":
            take()
            arguments = [parse_or()]
            while peek() == ",":
                take()
                arguments.append(parse_or())
            if take() != ")":
                raise AssertionError(f"unclosed format( in {expression!r}")
            result = str(arguments[0])
            for index, argument in enumerate(arguments[1:]):
                result = result.replace("{%d}" % index, str(argument))
            return result
        if token == "(":
            value = parse_or()
            take()
            return value
        if token.startswith("'"):
            return token[1:-1]
        if token in ("true", "false"):
            return token == "true"
        if token not in context:
            raise AssertionError(f"unknown context name {token!r} in {expression!r}")
        return context[token]

    value = parse_or()
    if position != len(tokens):
        raise AssertionError(f"cannot evaluate {expression!r}")
    return value


def workflow_text(workflow):
    return (WORKFLOWS / workflow).read_text(encoding="utf-8")


def step_block(workflow, name):
    return workflow_text(workflow).split(f"- name: {name}\n")[1].split("\n      - ")[0]


def step_condition(workflow, name):
    return re.search(r"^        if: (.*)$", step_block(workflow, name), re.MULTILINE).group(1)


def env_expression(workflow, step, variable):
    match = re.search(rf"^          {variable}: (.*)$", step_block(workflow, step), re.MULTILINE)
    return match.group(1)


def secret_interpolations_in_run(text):
    """Return `run` bodies (block or single line) that interpolate a secret."""
    found = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)(?:- )?run:\s*(.*)$", line)
        if not match:
            continue
        indent, rest = len(match.group(1)), match.group(2)
        body = [rest]
        if rest[:1] in ("|", ">"):
            body = []
            for following in lines[index + 1 :]:
                if following.strip() and len(following) - len(following.lstrip()) <= indent:
                    break
                body.append(following)
        if re.search(r"\$\{\{\s*secrets\.", "\n".join(body)):
            found.append("\n".join(body))
    return found


class UnbufferedSyncStepTest(unittest.TestCase):
    def test_python_sync_steps_print_unbuffered(self):
        for workflow, step in (
            ("sync-roadmap-issues.yml", "Synchronize roadmap issues"),
            ("sync-phase-issues.yml", "Synchronize Phase issues"),
            ("sync-project-status.yml", "Reconcile statuses"),
        ):
            with self.subTest(workflow=workflow):
                self.assertEqual("'1'", env_expression(workflow, step, "PYTHONUNBUFFERED"))


class BoardCredentialTest(unittest.TestCase):
    """The board accepts a GitHub App (preferred) or PROJECT_BOARD_TOKEN (personal-account boards)."""

    def test_the_issue_sync_board_token_follows_the_credential_order(self):
        cases = (
            # (app token, owner, PAT secret) -> BOARD_GH_TOKEN
            ("app-token", "example-org", "", "app-token"),
            ("app-token", "example-org", "pat", "app-token"),
            ("", "example-org", "pat", "pat"),
            ("", "", "pat", ""),
            ("", "example-org", "", ""),
            ("", "", "", ""),
        )
        for workflow, step in (
            ("sync-roadmap-issues.yml", "Synchronize roadmap issues"),
            ("sync-phase-issues.yml", "Synchronize Phase issues"),
        ):
            expression = env_expression(workflow, step, "BOARD_GH_TOKEN")
            for app_token, owner, pat, expected in cases:
                with self.subTest(workflow=workflow, app=app_token, owner=owner, pat=pat):
                    context = {
                        "steps.board.outputs.token": app_token,
                        "steps.board.outputs.owner": owner,
                        "secrets.PROJECT_BOARD_TOKEN": pat,
                    }
                    self.assertEqual(evaluate(expression, context), expected)

    def test_the_status_board_token_follows_the_credential_order(self):
        expression = env_expression("sync-project-status.yml", "Reconcile statuses", "BOARD_GH_TOKEN")
        cases = (
            ("app-token", "", "app-token"),
            ("app-token", "pat", "app-token"),
            ("", "pat", "pat"),
            ("", "", ""),
        )
        for app_token, pat, expected in cases:
            with self.subTest(app=app_token, pat=pat):
                context = {
                    "steps.board.outputs.token": app_token,
                    "secrets.PROJECT_BOARD_TOKEN": pat,
                }
                self.assertEqual(evaluate(expression, context), expected)

    def test_the_status_job_keeps_repository_reads_on_the_workflow_token(self):
        self.assertEqual(
            env_expression("sync-project-status.yml", "Reconcile statuses", "GH_TOKEN"),
            "${{ github.token }}",
        )
        text = workflow_text("sync-project-status.yml")
        permissions = text.split("permissions:\n")[1].split("\n\n")[0]
        for line in ("contents: read", "issues: read", "pull-requests: read"):
            self.assertIn(line, permissions)
        self.assertNotIn("write", permissions)

    def test_the_app_token_is_only_created_when_the_app_is_set(self):
        condition = action_step_condition(TOKEN_STEP)
        for client_id, key, runs in (
            ("id", "key", True),
            ("id", "", False),
            ("", "key", False),
            ("", "", False),
        ):
            with self.subTest(client_id=client_id, key=key):
                context = {
                    "inputs.client-id": client_id,
                    "inputs.private-key": key,
                    "steps.project-config.outputs.owner": "example-org",
                }
                self.assertEqual(bool(evaluate(condition, context)), runs)
        with self.subTest(owner="empty"):
            context = {
                "inputs.client-id": "id",
                "inputs.private-key": "key",
                "steps.project-config.outputs.owner": "",
            }
            self.assertFalse(evaluate(condition, context))

    def test_the_action_derives_the_credential_flags_from_its_inputs(self):
        # project_context.py reads HAS_BOARD_APP / HAS_BOARD_PAT from the environment; the action
        # sets them on the owner step from the inputs, so the callers carry no job env for them.
        step = action_step_block(OWNER_STEP)
        self.assertIn("HAS_BOARD_APP: ${{ inputs.client-id != '' && inputs.private-key != '' }}", step)
        self.assertIn("HAS_BOARD_PAT: ${{ inputs.has-board-pat }}", step)
        expression = "inputs.client-id != '' && inputs.private-key != ''"
        for client_id, key, expected in (("", "", False), ("id", "", False),
                                         ("", "key", False), ("id", "key", True)):
            with self.subTest(client_id=client_id, key=key):
                context = {"inputs.client-id": client_id, "inputs.private-key": key}
                self.assertIs(expected, bool(evaluate(expression, context)))
        for workflow in BOARD_WORKFLOWS:
            with self.subTest(workflow=workflow):
                text = workflow_text(workflow)
                self.assertNotIn("HAS_BOARD_", text)
                self.assertIn("client-id: ${{ secrets.PROJECT_STATUS_APP_ID }}", text)
                self.assertIn("private-key: ${{ secrets.PROJECT_BOARD_APP_PRIVATE_KEY }}", text)
                self.assertIn("has-board-pat: ${{ secrets.PROJECT_BOARD_TOKEN != '' }}", text)

    def test_the_issue_syncs_keep_issue_writes_on_the_workflow_token(self):
        for workflow in ISSUE_SYNCS:
            with self.subTest(workflow=workflow):
                self.assertIn("GH_TOKEN: ${{ github.token }}", workflow_text(workflow))

    def test_no_run_script_interpolates_a_secret(self):
        for workflow in sorted(WORKFLOWS.glob("*.yml")):
            with self.subTest(workflow=workflow.name):
                self.assertEqual(secret_interpolations_in_run(workflow.read_text(encoding="utf-8")), [])

    def test_the_evaluator_rejects_unsupported_syntax_and_unknown_names(self):
        with self.assertRaises(AssertionError):
            evaluate("${{ !(env.A == 'true') }}", {"env.A": "true"})
        with self.assertRaises(AssertionError):
            evaluate("${{ contains(env.A, 'x') }}", {"env.A": "x"})
        with self.assertRaises(AssertionError):
            evaluate("${{ env.MISSING == 'true' }}", {})
        self.assertTrue(evaluate("${{ env.A == 'true' }}", {"env.A": "true"}))

    def test_the_secret_guard_catches_block_and_single_line_runs(self):
        block = "steps:\n  - name: x\n    run: |\n      echo ${{ secrets.TOKEN }}\n"
        single = "steps:\n  - run: echo ${{ secrets.TOKEN }}\n"
        clean = "steps:\n  - run: echo hi\n    env:\n      T: ${{ secrets.TOKEN }}\n"
        self.assertEqual(len(secret_interpolations_in_run(block)), 1)
        self.assertEqual(len(secret_interpolations_in_run(single)), 1)
        self.assertEqual(secret_interpolations_in_run(clean), [])

    def test_the_status_workflow_stays_safe_under_pull_request_target(self):
        text = workflow_text("sync-project-status.yml")
        self.assertIn("persist-credentials: false", text)
        self.assertIn("github.event.pull_request.base.sha", text)


PREVIEW = "sync-roadmap-preview.yml"


def job_block(workflow, job):
    text = "\n" + workflow_text(workflow).split("\njobs:\n")[1]
    return re.split(r"(?m)^  \w[\w-]*:\n", text.split(f"\n  {job}:\n")[1])[0]


class RoadmapAutoApplyTest(unittest.TestCase):
    """A push to the default branch applies the roadmap sync once the first sync has run."""

    def group(self, event, apply="", variable=""):
        expression = re.search(
            r"^      group: (.*)$", workflow_text("sync-roadmap-issues.yml"), re.MULTILINE
        ).group(1)
        context = {"github.event_name": event, "inputs.apply": apply, "vars.ROADMAP_SYNC_AUTO_APPLY": variable}
        return evaluate(expression, context)

    def test_applying_runs_use_the_roadmap_group_and_previews_do_not(self):
        cases = (
            ("push", "", "", "issue-sync-roadmap"),
            ("push", "", "true", "issue-sync-roadmap"),
            ("push", "", "TRUE", "issue-sync-roadmap"),
            ("push", "", "false", "issue-sync-preview"),
            ("push", "", "False", "issue-sync-preview"),
            ("push", "", "FALSE", "issue-sync-preview"),
            # Padded: the group cannot trim, and the step rejects it, so the run fails either way.
            ("push", "", " false ", "issue-sync-roadmap"),
            ("workflow_dispatch", True, "", "issue-sync-roadmap"),
            ("workflow_dispatch", False, "", "issue-sync-preview"),
            ("schedule", "", "", "issue-sync-preview"),
        )
        for event, apply, variable, expected in cases:
            with self.subTest(event=event, apply=apply, variable=variable):
                self.assertEqual(expected, self.group(event, apply, variable))

    def test_one_merge_never_queues_two_jobs_in_one_single_slot_group(self):
        # A merge of a pull request that changes tasks.md starts the roadmap sync (push) and the
        # Phase sync (closed pull request). A concurrency group holds one running and one pending
        # job, so two jobs of one merge in one group would drop a run.
        roadmap = self.group("push", "", "")
        phase_text = workflow_text("sync-phase-issues.yml")
        expression = " ".join(
            re.search(r"group: >-\n((?:        .*\n)+)", phase_text).group(1).split()
        )
        phase = evaluate(
            expression,
            {
                "github.event_name": "pull_request",
                "inputs.apply": "",
                "vars.PHASE_SYNC_AUTO_APPLY": "",
            },
        )
        self.assertNotEqual(roadmap, phase)
        self.assertNotIn("issue-sync-preview", (roadmap, phase))

    def test_the_groups_are_documented_as_safe_to_run_side_by_side(self):
        text = workflow_text("sync-roadmap-issues.yml")
        self.assertIn("idempotent", text)
        self.assertIn("marker", text)

    def run_sync_step(self, event, auto_apply="", apply_requested="false", board="token"):
        script = step_script(
            "Synchronize roadmap issues", WORKFLOWS / "sync-roadmap-issues.yml"
        )
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory, "bin")
            bin_dir.mkdir()
            stub = bin_dir / "python3"
            stub.write_text('#!/bin/sh\necho "python3 $*"\n', encoding="utf-8")
            stub.chmod(0o755)
            env = {
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "GITHUB_EVENT_NAME": event,
                "AUTO_APPLY": auto_apply,
                "APPLY_REQUESTED": apply_requested,
                "BOARD_GH_TOKEN": board,
            }
            return subprocess.run(
                ["bash", "-e", "-c", script], env=env, capture_output=True, text=True, cwd=directory
            )

    def test_a_push_applies_unless_the_switch_is_false_in_any_case(self):
        for value in ("", "true", "TRUE", "True"):
            with self.subTest(value=value):
                result = self.run_sync_step("push", value)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn("--apply-if-synced", result.stdout)
        for value in ("false", "False", "FALSE"):
            with self.subTest(value=value):
                result = self.run_sync_step("push", value)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertNotIn("--apply", result.stdout)
                self.assertIn("Automatic apply is off", result.stdout)

    def test_any_other_switch_value_fails_a_push_run_with_a_clear_error(self):
        # Expressions cannot trim, so a padded value is invalid in bash too: both agree.
        for value in ("yes", "0", "off", "falsey", " false ", " true "):
            with self.subTest(value=value):
                result = self.run_sync_step("push", value)
                self.assertNotEqual(0, result.returncode)
                self.assertIn("ROADMAP_SYNC_AUTO_APPLY must be true or false", result.stderr)
                self.assertNotIn("python3", result.stdout)

    def test_schedule_never_applies_and_the_manual_input_is_unchanged(self):
        for value in ("", "false", "nonsense"):
            result = self.run_sync_step("schedule", value)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotIn("--apply", result.stdout)
        manual = self.run_sync_step("workflow_dispatch", "false", apply_requested="true")
        self.assertIn("--apply", manual.stdout)
        self.assertNotIn("--apply-if-synced", manual.stdout)
        preview = self.run_sync_step("workflow_dispatch", "", apply_requested="false")
        self.assertNotIn("--apply", preview.stdout)

    def test_the_workflow_never_cancels_a_running_sync(self):
        self.assertIn("cancel-in-progress: false", workflow_text("sync-roadmap-issues.yml"))
        self.assertNotIn("cancel-in-progress: true", workflow_text("sync-roadmap-issues.yml"))


class RoadmapPreviewWorkflowTest(unittest.TestCase):
    """Pull requests that change the roadmap or specs get a dry run, never an apply."""

    def test_it_runs_on_pull_requests_and_never_on_pull_request_target(self):
        text = workflow_text(PREVIEW)
        head = text.split("\njobs:\n")[0]
        self.assertRegex(head, r"(?m)^  pull_request:$")
        self.assertNotIn("pull_request_target", text)
        for path in ("ROADMAP.md", "specs/*/spec.md", "specs/*/plan.md", "specs/*/tasks.md"):
            self.assertIn(f"      - {path}\n", head)

    def test_the_watched_paths_match_the_apply_workflow(self):
        def paths(workflow):
            head = workflow_text(workflow).split("\njobs:\n")[0]
            return re.findall(r"(?m)^      - (ROADMAP\.md|specs/\S+)$", head)

        self.assertEqual(paths("sync-roadmap-issues.yml"), paths(PREVIEW))

    def test_the_preview_job_only_targets_the_default_branch(self):
        condition = re.search(r"^    if: (.*)$", job_block(PREVIEW, "preview"), re.MULTILINE).group(1)
        for base, runs in (("main", True), ("feature", False)):
            context = {
                "github.event.pull_request.base.ref": base,
                "github.event.repository.default_branch": "main",
            }
            with self.subTest(base=base):
                self.assertEqual(bool(evaluate(condition, context)), runs)

    def test_the_preview_job_is_read_only_and_has_no_secrets_or_board_credential(self):
        job = job_block(PREVIEW, "preview")
        # The dry run lists open pull requests (to tell a Phase in review) for closing entries.
        self.assertIn(
            "permissions:\n      contents: read\n      issues: read\n      pull-requests: read\n", job
        )
        self.assertNotIn("write", job.split("steps:")[0])
        self.assertNotIn("secrets.", job)
        self.assertNotIn("board-token", job)
        self.assertNotIn("BOARD_GH_TOKEN", job)
        self.assertIn("persist-credentials: false", job)
        self.assertIn("--skip-project", job)
        self.assertNotIn("--apply", job)
        self.assertIn("$GITHUB_STEP_SUMMARY", job)

    def test_the_dry_run_step_gives_gh_the_job_token(self):
        # sync-roadmap-issues.py calls `gh api`; without GH_TOKEN, gh stops in GitHub Actions (#120).
        self.assertEqual(
            "${{ github.token }}",
            env_expression(PREVIEW, "Preview the roadmap sync", "GH_TOKEN"),
        )

    def test_the_preview_workflow_is_wired_to_the_merge_commit_and_the_base_scripts(self):
        job = job_block(PREVIEW, "preview")
        # The checkout is the merge commit; the step reads its first parent and checks that out. The
        # event's base SHA is only compared with that parent, for a warning; nothing checks it out.
        self.assertIn("BASE_SHA: ${{ github.event.pull_request.base.sha }}", job)
        self.assertEqual(2, job.count("BASE_SHA"))
        self.assertIn('git checkout --detach "$PARENT_SHA"', job)
        self.assertIn("SOURCE_SHA: ${{ github.sha }}", job)
        self.assertNotIn("HEAD_SHA", job)
        self.assertIn('git -c "http.https://github.com/.extraheader=', job)
        self.assertIn('fetch --no-tags --depth=1 origin "$PARENT_SHA"', job)
        # No fallback: the merge commit is already checked out, and only the parent is fetched.
        self.assertNotIn("refs/pull/", job)
        self.assertNotIn("PR_NUMBER", job)
        self.assertIn("--report-sync-state", job)
        self.assertIn("AUTO_APPLY: ${{ vars.ROADMAP_SYNC_AUTO_APPLY }}", job)
        self.assertIn('--auto-apply "$AUTO_APPLY"', job)
        self.assertIn("CODE_CHANGED: ${{ steps.take.outputs.code_changed }}", job)
        # The data is taken before the dry run, which runs the base branch's script.
        self.assertLess(job.index("git checkout"), job.index("sync-roadmap-issues.py"))

    def test_the_diff_is_captured_before_it_is_searched(self):
        # Nothing pipes git diff into grep -q: a closed pipe could turn the result into "false".
        script = step_script("Take the roadmap and specs from the merge commit", WORKFLOWS / PREVIEW)
        self.assertNotRegex(script, r"diff[^\n]*\|")
        self.assertIn("^(ROADMAP\\.md$|specs/)", script)
        self.assertIn("test -L", script)
        self.assertIn("-- ROADMAP.md specs |", script)  # a pathspec without a trailing slash

    def test_the_header_explains_what_runs_and_who_can_change_it(self):
        head = workflow_text(PREVIEW).split("\njobs:\n")[0]
        self.assertIn("base branch", head)
        self.assertIn("read-only token", head)
        self.assertIn("merge commit", head)
        self.assertIn("only the roadmap and specs", head)
        # Same-repository pull requests run their own copy of this workflow file.
        self.assertIn("same repository", head)
        self.assertIn("review changes to .github/ and scripts/", head)
        self.assertIn("cannot change what runs", head)
        self.assertNotIn("A pull request cannot change what runs", head)

    # -- the step that takes the data from the merge commit, run against real git repositories --

    def git(self, cwd, *args):
        result = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
            cwd=cwd, capture_output=True, text=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def commit(self, work, files, remove=(), symlinks=None):
        for name in remove:
            path = Path(work, name)
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists() or path.is_symlink():
                path.unlink()
        for name, text in files.items():
            path = Path(work, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        for name, target in (symlinks or {}).items():
            path = Path(work, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(target, path)
        self.git(work, "add", "-A")
        self.git(work, "commit", "-m", "c", "--allow-empty")
        return self.git(work, "rev-parse", "HEAD")

    def take_step(self, make_source, as_merge=True, base_sha=None):
        """Run the step on a clone at the base commit; `make_source(work)` builds the source commit.

        Like GitHub's test merge commit, a single-parent source is wrapped in a merge commit with the
        base as first parent and the source as second, unless `as_merge` is false.
        """
        script = step_script("Take the roadmap and specs from the merge commit", WORKFLOWS / PREVIEW)
        with tempfile.TemporaryDirectory() as directory:
            origin = Path(directory, "origin.git")
            subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
            self.git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
            work = Path(directory, "work")
            work.mkdir()
            self.git(work, "init", "-q")
            base = self.commit(
                work,
                {
                    "ROADMAP.md": "base roadmap",
                    "specs/old/spec.md": "base spec",
                    "scripts/tool.py": "base script",
                    ".github/workflows/w.yml": "base workflow",
                    "project-context.toml": "base context",
                },
            )
            self.git(work, "remote", "add", "origin", str(origin))
            source = make_source(work)
            parents = self.git(work, "rev-list", "--parents", "-n", "1", source).split()[1:]
            if as_merge and len(parents) == 1:
                tree = self.git(work, "rev-parse", f"{source}^{{tree}}")
                source = self.git(work, "commit-tree", tree, "-p", base, "-p", source, "-m", "merge")
            self.git(work, "push", "-q", "origin", f"{source}:refs/pull/7/merge", f"{base}:refs/heads/main")
            # What actions/checkout leaves: a depth-1 checkout of the merge commit, parents not fetched.
            clone = Path(directory, "clone")
            clone.mkdir()
            self.git(clone, "init", "-q")
            self.git(clone, "remote", "add", "origin", str(origin))
            self.git(clone, "fetch", "-q", "--no-tags", "--depth=1", "origin", source)
            self.git(clone, "checkout", "-q", "--detach", source)
            output = Path(directory, "github_output")
            output.write_text("", encoding="utf-8")
            env = {
                "PATH": os.environ["PATH"],
                "HOME": directory,
                "GH_TOKEN": "t",
                "SOURCE_SHA": source,
                "BASE_SHA": base_sha or base,
                "GITHUB_OUTPUT": str(output),
            }
            result = subprocess.run(["bash", "-e", "-c", script], env=env, cwd=clone, capture_output=True, text=True)
            files = {
                str(p.relative_to(clone)): p.read_text(encoding="utf-8")
                for p in clone.rglob("*")
                if p.is_file() and not p.is_symlink() and ".git" not in p.relative_to(clone).parts
            }
            return result, files, output.read_text(encoding="utf-8")

    def test_the_step_takes_the_roadmap_and_specs_from_the_merge_commit_only(self):
        def source(work):
            return self.commit(
                work,
                {
                    "ROADMAP.md": "merged roadmap",
                    "specs/new/spec.md": "merged spec",
                    "scripts/tool.py": "evil script",
                    ".github/workflows/w.yml": "evil workflow",
                },
                remove=("specs/old",),
            )

        result, files, output = self.take_step(source)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("merged roadmap", files["ROADMAP.md"])
        self.assertEqual("merged spec", files["specs/new/spec.md"])
        self.assertNotIn("specs/old/spec.md", files)  # deleted by the pull request
        self.assertEqual("base script", files["scripts/tool.py"])
        self.assertEqual("base workflow", files[".github/workflows/w.yml"])
        self.assertIn("code_changed=true", output)

    def test_the_code_comes_from_the_first_parent_of_the_merge_commit(self):
        def source(work):
            base = self.git(work, "rev-parse", "HEAD")
            side = self.commit(
                work, {"scripts/tool.py": "head script", "ROADMAP.md": "merged roadmap"}
            )
            tree = self.git(work, "rev-parse", f"{side}^{{tree}}")
            return self.git(work, "commit-tree", tree, "-p", base, "-p", side, "-m", "merge")

        result, files, output = self.take_step(source)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("base script", files["scripts/tool.py"])  # the first parent, not the head
        self.assertEqual("merged roadmap", files["ROADMAP.md"])  # data from the merge commit
        self.assertIn("code_changed=true", output)

    def test_a_source_that_is_not_a_two_parent_merge_commit_is_refused(self):
        def source(work):
            return self.commit(work, {"ROADMAP.md": "merged roadmap"})

        result, _, _ = self.take_step(source, as_merge=False)
        self.assertEqual(1, result.returncode)
        self.assertIn("is not a merge commit with two parents", result.stderr)

    def test_a_parent_line_in_the_commit_message_is_not_read(self):
        def source(work):
            base = self.git(work, "rev-parse", "HEAD")
            tree = self.git(work, "rev-parse", f"{base}^{{tree}}")
            return self.git(work, "commit-tree", tree, "-p", base, "-m", f"x\n\nparent {base}")

        result, _, _ = self.take_step(source, as_merge=False)
        self.assertEqual(1, result.returncode)
        self.assertIn("is not a merge commit with two parents", result.stderr)

    def test_a_moved_base_is_reported_as_a_warning(self):
        def source(work):
            return self.commit(work, {"ROADMAP.md": "merged roadmap"})

        result, files, _ = self.take_step(source, base_sha="0" * 40)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("::warning::The base branch moved", result.stdout)
        self.assertEqual("merged roadmap", files["ROADMAP.md"])

    def test_a_source_commit_without_specs_or_roadmap_is_not_an_error(self):
        # The normal state of a bootstrap pull request: neither path exists in the source commit.
        for removed in (("specs",), ("ROADMAP.md",), ("specs", "ROADMAP.md")):
            with self.subTest(removed=removed):
                result, files, _ = self.take_step(
                    lambda work: self.commit(work, {"project-context.toml": "new"}, remove=removed)
                )
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual("base script", files["scripts/tool.py"])
                for name in removed:
                    self.assertFalse([f for f in files if f == name or f.startswith(f"{name}/")])

    def test_a_symlink_in_the_roadmap_or_specs_is_rejected_before_anything_is_taken(self):
        # Decision: reject (not core.symlinks=false), so the preview never silently reads a link.
        for link in ("specs/old/spec.md", "specs/link.md", "ROADMAP.md", "specs"):
            with self.subTest(link=link):
                result, files, output = self.take_step(
                    lambda work: self.commit(
                        work, {}, remove=(link,), symlinks={link: "/etc/hostname"}
                    )
                )
                self.assertNotEqual(0, result.returncode)
                self.assertIn("::error::", result.stdout + result.stderr)
                self.assertIn("symbolic link", result.stdout + result.stderr)
                self.assertEqual("base script", files["scripts/tool.py"])

    def test_the_code_changed_flag_covers_every_path_outside_the_roadmap_and_specs(self):
        cases = (
            ({"ROADMAP.md": "x", "specs/old/spec.md": "y"}, (), "false"),
            ({"specs/new/plan.md": "y"}, (), "false"),
            ({"scripts/tool.py": "x"}, (), "true"),
            ({".github/workflows/w.yml": "x"}, (), "true"),
            ({"project-context.toml": "x"}, (), "true"),
            ({".specify/memory/constitution.md": "x"}, (), "true"),
            ({"README.md": "x"}, (), "true"),
            # The data pattern is anchored: a file that only starts like ROADMAP.md is code.
            ({"ROADMAP.md.bak": "x"}, (), "true"),
            ({"specs-extra.md": "x"}, (), "true"),
            # A rename out of a code path: the old path is listed too.
            ({"specs/old/tool.py": "base script"}, ("scripts/tool.py",), "true"),
            # A rename from the data into a code path.
            ({"scripts/spec.md": "base spec"}, ("specs/old/spec.md",), "true"),
        )
        for changes, remove, expected in cases:
            with self.subTest(changes=list(changes), remove=remove):
                result, _, output = self.take_step(lambda work: self.commit(work, changes, remove=remove))
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertIn(f"code_changed={expected}", output)

    def test_the_preview_cancels_a_stale_run_but_the_comment_job_does_not(self):
        self.assertIn("cancel-in-progress: true", job_block(PREVIEW, "preview"))
        self.assertIn("cancel-in-progress: false", job_block(PREVIEW, "comment"))
        self.assertNotIn("cancel-in-progress: true", job_block(PREVIEW, "comment"))

    def test_the_comment_job_is_for_same_repository_pull_requests_and_runs_no_pr_code(self):
        job = job_block(PREVIEW, "comment")
        wrapped = re.search(r"^    if: (.*)$", job, re.MULTILINE).group(1)
        condition = wrapped.removeprefix("${{ ").removesuffix(" }}")
        self.assertIn("pull-requests: write", job)
        self.assertNotIn("contents:", job.split("steps:")[0])
        self.assertNotIn("actions/checkout", job)
        self.assertNotIn("secrets.", job)
        self.assertIn("<!-- roadmap-sync-preview -->", job)
        self.assertIn("github-actions[bot]", job)
        self.assertIn("-X PATCH", job)
        for head, runs in (("o/r", True), ("fork/r", False)):
            with self.subTest(head=head):
                context = {
                    "github.event.pull_request.head.repo.full_name": head,
                    "github.repository": "o/r",
                    "needs.preview.outputs.body": "text",
                }
                self.assertEqual(bool(evaluate(condition.replace("!cancelled() && ", ""), context)), runs)
        self.assertTrue(condition.startswith("!cancelled() && "))
        self.assertNotIn("always()", workflow_text(PREVIEW).replace("`always()`", ""))
        self.assertIn("HEAD_SHA: ${{ github.event.pull_request.head.sha }}", job)
        self.assertIn("MERGE_SHA: ${{ github.sha }}", job)

    def test_both_jobs_use_a_per_pull_request_group(self):
        text = workflow_text(PREVIEW)
        self.assertRegex(text, r"group: issue-sync-preview-pr-\$\{\{ github\.event\.pull_request\.number \}\}\n")
        self.assertRegex(
            text, r"group: issue-sync-preview-pr-\$\{\{ github\.event\.pull_request\.number \}\}-comment\n"
        )

    def test_the_preview_step_drops_fence_lookalikes_and_fences_the_summary(self):
        script = step_script("Preview the roadmap sync", WORKFLOWS / PREVIEW)
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory, "bin")
            bin_dir.mkdir()
            stub = bin_dir / "python3"
            stub.write_text(
                "#!/bin/sh\n"
                'case "$1" in\n'
                f'  scripts/sync-roadmap-issues.py) printf "[dry-run] create x\\n   ~~~~\\n~~~~~~\\n" ;;\n'
                f'  *) exec "{sys.executable}" "$@" ;;\n'
                "esac\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)
            summary, output = Path(directory, "summary"), Path(directory, "output")
            summary.write_text("")
            output.write_text("")
            env = {
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "RUNNER_TEMP": directory,
                "GITHUB_STEP_SUMMARY": str(summary),
                "GITHUB_OUTPUT": str(output),
                "CODE_CHANGED": "true",
                "AUTO_APPLY": "",
                "GH_TOKEN": "t",
            }
            result = subprocess.run(["bash", "-e", "-c", script], env=env, cwd=root, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            for name in (summary, output):
                text = name.read_text(encoding="utf-8")
                fences = [line for line in text.splitlines() if re.match(r" {0,3}~{3,}", line)]
                self.assertEqual(2 if name == summary else 0, len(fences), text)
            self.assertIn("changes files other than ROADMAP.md and specs/", output.read_text(encoding="utf-8"))

    def test_both_fence_filters_use_the_commonmark_closing_fence_rule(self):
        text = workflow_text(PREVIEW)
        self.assertEqual(2, text.count("sed -E -e '/^ {0,3}~{3,}/d'"))

    def test_the_summary_is_written_to_the_output_with_a_random_delimiter(self):
        job = job_block(PREVIEW, "preview")
        self.assertIn("openssl rand", job)
        self.assertIn("$GITHUB_OUTPUT", job)
        self.assertNotIn("roadmap-sync-preview", job)  # the comment job owns the marker

    def run_comment_step(self, body, existing_id=""):
        script = step_script("Create or update the preview comment", WORKFLOWS / PREVIEW)
        with tempfile.TemporaryDirectory() as directory:
            bin_dir = Path(directory, "bin")
            bin_dir.mkdir()
            stub = bin_dir / "gh"
            stub.write_text(
                "#!/usr/bin/env python3\n"
                "import sys, os, re\n"
                "args = sys.argv[1:]\n"
                "log = open(os.environ['STUB_LOG'], 'a')\n"
                "log.write(' '.join(args) + '\\n')\n"
                "for arg in args:\n"
                "    match = re.match(r'body=@(.*)', arg)\n"
                "    if match:\n"
                "        log.write('BODY<<' + open(match.group(1)).read() + '>>BODY\\n')\n"
                "if '--jq' in args and '-X' not in args:\n"
                "    print(os.environ.get('STUB_ID', ''))\n",
                encoding="utf-8",
            )
            stub.chmod(0o755)
            log = Path(directory, "log")
            env = {
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "STUB_LOG": str(log),
                "STUB_ID": existing_id,
                "GH_TOKEN": "t",
                "GITHUB_REPOSITORY": "o/r",
                "PR_NUMBER": "7",
                "HEAD_SHA": "a" * 40,
                "MERGE_SHA": "b" * 40,
                "RUNNER_TEMP": directory,
                "BODY": body,
            }
            result = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            return log.read_text(encoding="utf-8")

    def comment_body(self, log):
        return log.split("BODY<<", 1)[1].split(">>BODY", 1)[0]

    def test_the_comment_job_adds_the_marker_and_the_head_sha_itself(self):
        log = self.run_comment_step("[dry-run] create Feature: x\n")
        body = self.comment_body(log)
        self.assertTrue(body.startswith("<!-- roadmap-sync-preview -->\n"))
        self.assertIn("merge commit", body.split("~~~~", 1)[0])
        self.assertIn("b" * 40, body)  # the merge commit the data came from
        self.assertIn("a" * 40, body)  # the pull request head, for reference
        self.assertIn("-X POST", log)
        self.assertIn("[dry-run] create Feature: x", body)

    def test_the_comment_job_updates_the_existing_comment_in_place(self):
        log = self.run_comment_step("[dry-run] create Feature: x\n", existing_id="1234")
        self.assertIn("-X PATCH repos/o/r/issues/comments/1234", log)
        self.assertNotIn("-X POST", log)

    def test_the_comment_job_neutralises_mentions_markup_and_a_forged_marker(self):
        hostile = (
            "[dry-run] create @octocat and @org/team <img src=x> <!-- roadmap-sync-preview -->\n"
            "~~~~\n"
            "   ~~~~\n"
            "~~~~~~\n"
            "  ~~~~~~~~text\n"
            "[dry-run] ![t](https://example.invalid/p.png)\n"
        )
        body = self.comment_body(self.run_comment_step(hostile))
        self.assertEqual(1, body.count("<!-- roadmap-sync-preview -->"))
        self.assertNotRegex(body, r"@(?!\u200b)\w")
        self.assertNotIn("<img", body)
        # Only the fence the comment job writes: no line a CommonMark parser could take as a fence
        # (up to three spaces of indentation, three or more tildes).
        fence_lines = [line for line in body.splitlines() if re.match(r" {0,3}~{3,}", line)]
        self.assertEqual(2, len(fence_lines))

    def test_the_comment_job_caps_the_length_at_a_line_end(self):
        # About 66 KB: over the job's 50000 cap, but under Linux's 128 KB limit for one environment
        # variable (the body reaches the step through `env`; the preview job caps it at 60000 first).
        lines = "".join(f"[dry-run] create Feature: {n} {'x' * 80}\n" for n in range(600))
        body = self.comment_body(self.run_comment_step(lines))
        self.assertLess(len(body), 62000)
        shown = [line for line in body.splitlines() if line.startswith("[dry-run]")]
        self.assertTrue(all(line.endswith("x" * 80) for line in shown))
        self.assertIn("truncated", body)


if __name__ == "__main__":
    unittest.main()
