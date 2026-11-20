from pathlib import Path
import json
import re
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


if __name__ == "__main__":
    unittest.main()
