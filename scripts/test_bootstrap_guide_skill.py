"""Reference test for the `bootstrap-guide` skill (.agents/skills/bootstrap-guide/SKILL.md).

The skill is prose that an agent follows, so what it names must not rot. This test extracts from
the skill and checks against the engine and `docs/bootstrap.md`:

- every `python3 scripts/...` command: the script exists, a `bootstrap.py` subcommand and its
  `--flags` exist;
- every `docs/bootstrap.md#anchor`: the heading exists;
- every `check --json` name in a code span: a report field (`format_version`), a step field
  (`steps[].field`), a details key (`details.key`) and a bare snake_case word must be a step ID or
  a name the docs list in "Machine-readable status". Accepted trade-off: only names with an
  underscore are taken for `check --json` names, so the one-word step IDs (`constitution`,
  `roadmap`, `prerequisites`) are not checked in prose; the catalogue-versus-docs ID check still
  covers them;
- every relative link resolves from the skill's directory, with its anchor;
- the format version the skill knows equals the engine's (`format version N`);
- every `bootstrap.py start` command passes `--no-input`, and every `--confirm` uses the
  `<step-id>@<digest>` form.

The guard tests run each check on a deliberately broken copy and require it to fail.

What the skill must say and must never say (secrets, prohibited commands, the rules and the
revoke steps in the docs) is checked in `test_bootstrap_guide_safety.py`, which imports the docs and
engine helpers from this file (docs section lookup, the engine option lookup). The generic
skill-text helpers (section and heading parsing, the code-span and code-block patterns, link
checking) live in `skill_text_helpers.py`.
"""

import argparse
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import bootstrap  # noqa: E402
import bootstrap_steps  # noqa: E402
from skill_text_helpers import CODE_SPAN, FENCE, check_links, doc_anchors  # noqa: E402

SKILL = ROOT / ".agents" / "skills" / "bootstrap-guide" / "SKILL.md"
DOC = ROOT / "docs" / "bootstrap.md"
DOC_SECTION = "Machine-readable status"

# A command ends at the closing backtick or the line end, so a command in a code span that wraps
# over two lines would be cut short: `check_no_wrapped_commands` rejects that.
COMMAND = re.compile(r"python3 scripts/[^\s`]+(?:[^\n`]*)")
WRAPPED_SPAN = re.compile(r"`[^`\n]*python3 scripts/[^`]*\n[^`]*`")
ANCHOR = re.compile(r"docs/bootstrap\.md#([A-Za-z0-9_-]+)")
# Only names with an underscore are taken for `check --json` names (`next_step`, `project_context`);
# a single word such as `project` or `start` is ordinary prose.
SNAKE = re.compile(r"^[a-z]+(?:_[a-z]+)+$")
STEP_FIELD = re.compile(r"^steps\[\]\.([a-z_]+)$")
DETAILS_KEY = re.compile(r"^details\.([a-z_]+)$")
ACTION_MEMBER = re.compile(r"^action\.([a-z_]+)$")
ACTION_MEMBERS = {"text", "command", "doc"}  # contracts/check-json.md, data-model.md
FORMAT_VERSION = re.compile(r"format version (\d+)", re.I)
CONFIRM_VALUE = re.compile(r"--confirm[ =]([^\s;]+)")
# FR-019b: the one suffix a command the agent runs itself may carry, and nothing else after it
EXIT_SUFFIX = '; echo "exit $?"'
# a command may carry the suffix and nothing else: no chaining, expansion, redirection, comment or
# grouping; the `<step-id>`, `<digest>`, `<tools>` placeholders are blanked before this is applied
SHELL_CONTROL = re.compile(r"[;&|$`<>()\\#]")
PLACEHOLDER = re.compile(
    r"(?<=--agents )<tools>(?=\s|$)|(?<=--confirm[ =])<step-id>@<digest>(?=\s|$)"
)
FLAG = re.compile(r"(?<![\w-])(--[a-z][a-z-]*)")
FLAG_SPAN = re.compile(r"^(--[a-z][a-z-]*)(?:[ =].*)?$")
# Underscore names that are not `check --json` names (for example the GitHub token scope
# `public_repo`); add them here when the skill names one. They must never be a step ID or a
# details key. Scopes spelled with a colon (`read:org`) never match SNAKE.
NON_JSON_NAMES = {"public_repo"}


def doc_section(text, title):
    """The text of the `## title` section, up to the next `## ` heading."""
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    if not match:
        raise AssertionError(f"docs/bootstrap.md has no section {title!r}")
    return match.group(1)


def subsection(section, heading):
    match = re.search(rf"^### {re.escape(heading)}\n(.*?)(?=^### |\Z)", section, re.S | re.M)
    if not match:
        raise AssertionError(f"docs/bootstrap.md has no subsection {heading!r}")
    return match.group(1)


def table_first_column(section, heading):
    """Backticked names in the first column of the table under `### heading`."""
    names = set()
    for line in subsection(section, heading).splitlines():
        cells = line.split("|")
        if len(cells) > 2:
            names.update(re.findall(r"`([a-z_]+)`", cells[1]))
    return names


def known_names(doc_text):
    """What the skill may name: report fields, step fields, details keys, step IDs, values."""
    section = doc_section(doc_text, DOC_SECTION)
    report = table_first_column(section, "Report fields")
    step = table_first_column(section, "Step fields")
    details = set()
    doc_ids = set()
    for line in subsection(section, "Steps and details keys").splitlines():
        cells = line.split("|")
        if len(cells) > 2:
            # a key is the backticked name before a colon; values in the cell are not keys
            details.update(re.findall(r"`([a-z_]+)`:", cells[2]))
            doc_ids.update(re.findall(r"`([a-z_]+)`", cells[1]))
    ids = {entry.id for entry in bootstrap_steps.CATALOGUE}
    # values (`todo`, `complete`, ...) and other snake_case words the docs name in this section
    words = set(re.findall(r"`([a-z]+(?:_[a-z]+)*)`", section))
    return {"report": report, "step": step, "details": details, "ids": ids,
            "doc_ids": doc_ids, "words": words | ids}


def bootstrap_options():
    """Subcommand -> its option strings, from the engine's own parsers."""
    parser = bootstrap.build_parser()
    choices = next(
        action.choices for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    options = {}
    for name, sub in choices.items():
        options[name] = {s for action in sub._actions for s in action.option_strings}
    # `start` is listed for --help only; its options live in bootstrap_start.py
    import bootstrap_start

    options["start"] = {
        s for action in bootstrap_start.build_parser()._actions for s in action.option_strings
    }
    return options


def check_commands(text, options):
    errors = []
    for command in COMMAND.findall(text):
        command = command.strip()
        words = command.split()
        script = ROOT / words[1]
        if not script.is_file():
            errors.append(f"script does not exist: {words[1]}")
            continue
        if words[1] != "scripts/bootstrap.py":
            continue
        rest = [w for w in words[2:] if not w.startswith(("-", "<", "["))]
        sub = rest[0] if rest else "check"
        if sub not in options:
            errors.append(f"unknown bootstrap.py subcommand {sub!r} in: {command}")
            continue
        for flag in FLAG.findall(" ".join(words[2:])):
            if flag not in options[sub] and flag not in {"--help"}:
                errors.append(f"unknown flag {flag} for {sub} in: {command}")
    return errors


def check_anchors(text, anchors):
    return [f"no heading for docs/bootstrap.md#{a}" for a in ANCHOR.findall(text) if a not in anchors]


def check_names(text, names):
    errors = []
    prose = FENCE.sub("", text)
    for span in CODE_SPAN.findall(prose):
        span = span.strip()
        if (m := STEP_FIELD.match(span)) and m.group(1) not in names["step"]:
            errors.append(f"unknown step field: {span}")
        elif (m := DETAILS_KEY.match(span)) and m.group(1) not in names["details"]:
            errors.append(f"unknown details key: {span}")
        elif (m := ACTION_MEMBER.match(span)) and m.group(1) not in ACTION_MEMBERS:
            errors.append(f"unknown action member: {span}")
        elif SNAKE.match(span) and span not in (
            names["words"] | names["report"] | names["step"] | names["details"] | NON_JSON_NAMES
        ):
            errors.append(f"unknown name: {span}")
    if names["doc_ids"] != names["ids"]:
        errors.append(f"docs step IDs {sorted(names['doc_ids'])} differ from the catalogue "
                      f"{sorted(names['ids'])}")
    return errors


def check_flags(text, options):
    """Every code span that is a `--flag` (outside `python3 scripts/` commands, which
    `check_commands` covers) must be an option of `start` or of a `bootstrap.py` subcommand."""
    known = set().union(*options.values())
    errors = []
    for span in CODE_SPAN.findall(FENCE.sub("", text)):
        match = FLAG_SPAN.match(span.strip())
        if match and match.group(1) not in known:
            errors.append(f"unknown flag: {span}")
    return errors


def check_frontmatter(text):
    match = re.match(r"---\n(.*?)\n---\n", text, re.S)
    if not match:
        return ["no frontmatter"]
    fields = dict(re.findall(r"(?m)^([a-z-]+):[ \t]*(.*)$", match.group(1)))
    errors = []
    if fields.get("name") != "bootstrap-guide":
        errors.append(f"frontmatter name must be bootstrap-guide, got {fields.get('name')!r}")
    if not fields.get("description", "").strip():
        errors.append("frontmatter description is empty or missing")
    return errors


def check_stops_on_higher_version(text):
    """One paragraph must tie `format_version`, a higher value and a stop together."""
    for paragraph in re.split(r"\n\s*\n", text):
        lowered = paragraph.lower()
        if "`format_version`" in paragraph and "higher" in lowered and "stop" in lowered:
            return []
    return ["no paragraph says to stop when `format_version` is higher than the skill knows"]


def check_preview_exit_rule(text):
    """The preview run can exit with a code other than 3 (a stop, missing flags, complete); one
    paragraph must say that no yes is asked then."""
    for paragraph in re.split(r"\n\s*\n", text):
        flat = " ".join(paragraph.split()).lower()
        if "exits with anything other than 3" in flat and "do not ask for a yes" in flat:
            return []
    return ["no paragraph says not to ask for a yes when the preview exits with anything other "
            "than 3"]


def check_format_version(text):
    found = FORMAT_VERSION.findall(text)
    if not found:
        return ["the skill does not state the format version it knows ('format version N')"]
    return [
        f"skill knows format version {v}, engine writes {bootstrap_steps.FORMAT_VERSION}"
        for v in found if int(v) != bootstrap_steps.FORMAT_VERSION
    ]


def check_start_and_confirm(text, ids):
    errors = []
    for command in COMMAND.findall(text):
        if re.search(r"bootstrap\.py\s+start\b", command) and "--no-input" not in command:
            errors.append(f"start without --no-input: {command.strip()}")
    for command in COMMAND.findall(text):
        for value in CONFIRM_VALUE.findall(command):
            step, _, digest = value.partition("@")
            if not ((step == "<step-id>" or step in ids) and digest == "<digest>"):
                errors.append(f"--confirm value not in <step-id>@<digest> form: {value}")
    return errors


def check_exit_suffix(text):
    """After a `python3 scripts/` command only the fixed `; echo "exit $?"` suffix is allowed
    (FR-019b, research D20): any other `;`, `&&`, `|`, `$` or backtick chain is an error, and
    so is a suffix that is not exactly the fixed one."""
    errors = []
    for match in COMMAND.finditer(text):
        command = match.group(0).strip()
        bang = bool(re.search(r"!\s*$", text[: match.start()]))  # the `!` form is outside the match
        with_suffix = command.endswith(EXIT_SUFFIX)
        base = command[: -len(EXIT_SUFFIX)] if with_suffix else command
        if SHELL_CONTROL.search(PLACEHOLDER.sub("", base)):
            errors.append(f"command carries more than the fixed exit suffix: {command}")
        if with_suffix and ("--guided" in base or bang):
            errors.append(f"command shown to the user carries the exit suffix: {command}")
    return errors


def check_no_wrapped_commands(text):
    """A command in a code span must sit on one line, or COMMAND would cut it short."""
    return [f"command code span wraps over two lines: {m.group(0)!r}"
            for m in WRAPPED_SPAN.finditer(FENCE.sub("", text))]


def command_blocks(text):
    """The non-empty lines of each fenced block (also inside a `> ` quote), `> ` removed."""
    blocks, block, opener = [], None, ""
    for line in text.splitlines():
        bare = re.sub(r"^(?:>\s?)+", "", line).strip()
        fence = re.match(r"(`{3,}|~{3,})", bare)
        if block is None and fence:
            block, opener = [], fence.group(1)
        elif block is not None and fence and fence.group(1)[0] == opener[0] and (
            len(fence.group(1)) >= len(opener) and bare == fence.group(1)
        ):
            blocks.append(block)
            block = None
        elif block is not None and bare:
            block.append(bare)
    return blocks


def check_fenced_command_lines(text):
    """COMMAND finds only the `python3 scripts/` part of a line, so in a fenced block that holds
    one, every line must be a command (optionally with the `! ` prefix) that carries at most the
    fixed suffix: a second line such as `rm -rf x` or `gh secret set X` is an error."""
    errors = []
    for block in command_blocks(text):
        if not any("python3 scripts/" in line for line in block):
            continue
        for line in block:
            line = re.sub(r"^!\s*", "", line)
            base = line[: -len(EXIT_SUFFIX)] if line.endswith(EXIT_SUFFIX) else line
            if not line.startswith("python3 scripts/") or SHELL_CONTROL.search(
                PLACEHOLDER.sub("", base)
            ):
                errors.append(f"line in a command block is not an accepted command: {line}")
    return errors


def check_all(text, doc_text=None, options=None):
    doc_text = DOC.read_text() if doc_text is None else doc_text
    names = known_names(doc_text)
    options = options or bootstrap_options()
    return (
        check_frontmatter(text)
        + check_commands(text, options)
        + check_flags(text, options)
        + check_links(text, SKILL)
        + check_stops_on_higher_version(text)
        + check_preview_exit_rule(text)
        + check_anchors(text, doc_anchors(doc_text))
        + check_names(text, names)
        + check_format_version(text)
        + check_start_and_confirm(text, names["ids"])
        + check_exit_suffix(text)
        + check_no_wrapped_commands(text)
        + check_fenced_command_lines(text)
    )


class SkillReferenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = SKILL.read_text()
        cls.doc = DOC.read_text()
        cls.options = bootstrap_options()

    def test_references_exist(self):
        self.assertEqual([], check_all(self.text, self.doc, self.options))

    def test_skill_uses_each_kind_of_reference(self):
        # an extraction that finds nothing would pass every check above
        self.assertTrue(COMMAND.findall(self.text), "no python3 scripts/ command found")
        self.assertTrue(ANCHOR.findall(self.text), "no docs/bootstrap.md#anchor found")
        self.assertIn("`details.candidates`", self.text)
        self.assertIn("`steps[].performable`", self.text)
        self.assertTrue(FORMAT_VERSION.findall(self.text))
        self.assertTrue(CONFIRM_VALUE.findall(self.text), "no --confirm command found")
        for step in ("bootstrap_issue", "board_workflows"):
            self.assertIn(f"`{step}`", self.text)

    def test_skill_names_flags_and_the_engines_confirm_line(self):
        self.assertIn("`--issue N`", self.text)
        self.assertIn("To do it, run:", self.text)
        self.assertNotIn("To proceed, rerun with", self.text)
        self.assertNotIn("To use #", self.text)
        self.assertIn("`--guided`", self.text)

    def test_skill_names_the_fixed_exit_suffix_and_runs_commands_with_it(self):
        self.assertIn(f"`{EXIT_SUFFIX}`", self.text)
        with_suffix = [c for c in COMMAND.findall(self.text) if c.strip().endswith(EXIT_SUFFIX)]
        self.assertTrue(with_suffix, "no command in the skill carries the fixed suffix")

    def test_skill_does_not_copy_the_engines_command_into_the_preview(self):
        # second guide-me pilot (D18): the copy stops before "To do it, run:"
        flat = " ".join(self.text.split())
        self.assertIn("to the line before `To do it, run:`", flat)
        self.assertNotIn("to its `confirm:` line", flat)
        self.assertNotIn('The "Manual step" line in the `github_settings` preview', flat)


class GuardTest(unittest.TestCase):
    """Each assertion must fail on a deliberately broken copy of the skill."""

    @classmethod
    def setUpClass(cls):
        cls.text = SKILL.read_text()
        cls.doc = DOC.read_text()
        cls.options = bootstrap_options()
        cls.names = known_names(cls.doc)

    def broken(self, addition):
        return self.text + "\n" + addition + "\n"

    def test_missing_script(self):
        errors = check_commands(self.broken("`python3 scripts/nope.py check`"), self.options)
        self.assertTrue(errors)

    def test_unknown_subcommand_and_flag(self):
        self.assertTrue(check_commands(self.broken("`python3 scripts/bootstrap.py frobnicate`"),
                                       self.options))
        self.assertTrue(check_commands(self.broken("`python3 scripts/bootstrap.py check --bogus`"),
                                       self.options))

    def test_missing_anchor(self):
        text = self.broken("[x](docs/bootstrap.md#no-such-heading)")
        self.assertTrue(check_anchors(text, doc_anchors(self.doc)))

    def test_unknown_names(self):
        self.assertTrue(check_names(self.broken("`steps[].bogus_field`"), self.names))
        self.assertTrue(check_names(self.broken("`details.bogus_key`"), self.names))
        self.assertTrue(check_names(self.broken("`bogus_step`"), self.names))

    def test_step_id_dropped_from_catalogue_is_caught(self):
        names = dict(self.names, ids=self.names["ids"] - {"board_workflows"})
        errors = check_names(self.text, names)
        self.assertTrue(any("board_workflows" in e for e in errors), errors)

    def test_removed_candidates_key_is_caught(self):
        names = dict(self.names, details=self.names["details"] - {"candidates"})
        self.assertTrue(check_names(self.text, names))

    def test_format_version_mismatch_or_absent(self):
        self.assertTrue(check_format_version(self.broken("format version 99")))
        stripped = FORMAT_VERSION.sub("format version", self.text)
        self.assertTrue(check_format_version(stripped))

    def test_start_without_no_input(self):
        text = self.broken("`python3 scripts/bootstrap.py start`")
        self.assertTrue(check_start_and_confirm(text, self.names["ids"]))

    def test_confirm_without_digest_form(self):
        for bad in ("pull_request", "pull_request@", "bogus@<digest>", "<step-id>@abc123"):
            text = self.broken(f"`python3 scripts/bootstrap.py start --no-input --confirm {bad}`")
            self.assertTrue(check_start_and_confirm(text, self.names["ids"]), bad)

    def test_review_notes_flag_is_accepted_on_the_confirming_command(self):
        # research D21: the notes flag goes to the pull_request preview and the confirming command
        command = ("python3 scripts/bootstrap.py start --no-input --confirm "
                   "<step-id>@<digest> --review-notes --guided")
        for text in (self.broken(f"`{command}`"), self.broken(f"```text\n! {command}\n```"),
                     self.broken("`python3 scripts/bootstrap.py start --no-input --review-notes`")):
            self.assertEqual([], check_commands(text, self.options))
            self.assertEqual([], check_exit_suffix(text))
            self.assertEqual([], check_start_and_confirm(text, self.names["ids"]))
            self.assertEqual([], check_fenced_command_lines(text))
        self.assertEqual([], check_flags(self.broken("`--review-notes`"), self.options))

    def test_exit_suffix_is_the_only_thing_allowed_after_a_command(self):
        self.assertEqual([], check_exit_suffix(self.text))
        for good in (f"`python3 scripts/bootstrap.py check --json{EXIT_SUFFIX}`",
                     f"`python3 scripts/bootstrap.py start --no-input --confirm "
                     f"<step-id>@<digest>{EXIT_SUFFIX}`"):
            self.assertEqual([], check_exit_suffix(self.broken(good)), good)
            self.assertEqual([], check_start_and_confirm(self.broken(good), self.names["ids"]))
        for bad in ('; echo done', '; echo "exit"', '; echo "exit $?" ; true', '&& echo "exit $?"',
                    '; echo "exit $?"; echo again', '| cat', '; echo "$TOKEN"', '; echo `date`'):
            text = self.broken(f"`python3 scripts/bootstrap.py check --json{bad}`")
            self.assertTrue(check_exit_suffix(text), bad)
        # redirection, comments, grouping and escapes are not allowed either
        for bad in (" > /tmp/x", " 2> /tmp/x", " < /dev/null", " # note", " (x)", " \\x",
                    f" > /tmp/x{EXIT_SUFFIX}"):
            text = self.broken(f"`python3 scripts/bootstrap.py check --json{bad}`")
            self.assertTrue(check_exit_suffix(text), bad)
        # only the skill's own placeholders are blanked, as a whole argument or confirm part
        for bad in (" <in> out", " <a>/tmp/x", " --agents <tools>/tmp/x", " <tools>x",
                    " --confirm <step-id>@<digest>/x", " <tools> out", " <digest> out",
                    " @<digest> out", " =<tools> x", " <step-id>@<step-id> x",
                    " --confirm <step-id>@<digest> <tools>"):
            text = self.broken(f"`python3 scripts/bootstrap.py start --no-input{bad}`")
            self.assertTrue(check_exit_suffix(text), bad)
        # a command shown to the user (`--guided`, or the `!` form) never carries the suffix
        for bad in (f"python3 scripts/bootstrap.py start --no-input --confirm "
                    f"<step-id>@<digest> --guided{EXIT_SUFFIX}",
                    f"! python3 scripts/bootstrap.py check --json{EXIT_SUFFIX}",
                    f"!python3 scripts/bootstrap.py check --json{EXIT_SUFFIX}",
                    f"!  python3 scripts/bootstrap.py check --json{EXIT_SUFFIX}"):
            self.assertTrue(check_exit_suffix(self.broken(f"`{bad}`")), bad)
            self.assertTrue(check_exit_suffix(self.broken(f"```text\n{bad}\n```")), bad)
        # the combined check sees a bad suffix, so dropping it from check_all would fail here
        broken = self.broken("`python3 scripts/bootstrap.py check --json; echo done`")
        self.assertTrue(check_all(broken, self.doc, self.options))
        # a command span wrapped over two lines is cut short by COMMAND, so it is rejected
        self.assertEqual([], check_no_wrapped_commands(self.text))
        wrapped = self.broken("`python3 scripts/bootstrap.py check\n--json; rm x`")
        self.assertTrue(check_no_wrapped_commands(wrapped))
        self.assertTrue(check_all(wrapped, self.doc, self.options))
        # every line of a block that holds a command is checked, not only the command itself
        self.assertEqual([], check_fenced_command_lines(self.text))
        self.assertTrue(command_blocks(self.text), "no fenced block found")
        for extra in ("rm -rf x", "gh secret set X --body y", "echo hi; rm x",
                      "python3 scripts/bootstrap.py check --json && rm x"):
            for prefix in ("", "> "):
                block = (f"{prefix}```text\n{prefix}! python3 scripts/bootstrap.py check --json\n"
                         f"{prefix}{extra}\n{prefix}```")
                self.assertTrue(check_fenced_command_lines(self.broken(block)), (prefix, extra))
                self.assertTrue(check_all(self.broken(block), self.doc, self.options))
        # a tilde fence (three or more) and a longer backtick fence are blocks too
        for fence in ("~~~", "~~~~", "````"):
            block = (f"{fence}text\npython3 scripts/bootstrap.py check --json\nrm -rf x\n{fence}")
            self.assertTrue(check_fenced_command_lines(self.broken(block)), fence)
        # a block without a command is not a command block (the completion summary layout)
        self.assertEqual([], check_fenced_command_lines(self.broken("```text\nrm x\n```")))
        # a suffix on its own line in a block is still checked
        text = self.broken('```text\npython3 scripts/bootstrap.py check --json; rm -rf x\n```')
        self.assertTrue(check_exit_suffix(text))
        # the digest of a --confirm value is still checked when the suffix follows it
        bad = f"`python3 scripts/bootstrap.py start --no-input --confirm x@abc{EXIT_SUFFIX}`"
        self.assertTrue(check_start_and_confirm(self.broken(bad), self.names["ids"]))

    def test_doc_anchor_extraction_ignores_code_comments(self):
        anchors = doc_anchors("# Title\n```bash\n# not a heading\n```\n## Real `one`.toml\n")
        self.assertIn("real-onetoml", anchors)
        self.assertNotIn("not-a-heading", anchors)

    def test_details_key_must_be_a_key_not_a_value(self):
        for bogus in ("details.state", "details.result", "details.true", "details.merged"):
            self.assertTrue(check_names(self.broken(f"`{bogus}`"), self.names), bogus)

    def test_unknown_flag_in_prose(self):
        self.assertTrue(check_flags(self.broken("`--bogus`"), self.options))
        self.assertTrue(check_flags(self.broken("`--bogus N`"), self.options))

    def test_flag_in_prose_is_found_when_removed_from_the_engine(self):
        options = {name: flags - {"--issue"} for name, flags in self.options.items()}
        self.assertTrue(check_flags(self.text, options))

    def test_unknown_action_member(self):
        self.assertTrue(check_names(self.broken("`action.bogus`"), self.names))
        self.assertFalse(check_names(self.broken("`action.command`"), self.names))

    def test_unknown_underscore_name_but_not_plain_words(self):
        self.assertTrue(check_names(self.broken("`bogus_name`"), self.names))
        self.assertFalse(check_names(self.broken("`project` `public_repo` `start`"), self.names))
        # real scope spellings contain a colon and are never taken for JSON names
        self.assertFalse(check_names(self.broken("`read:org` `admin:org`"), self.names))
        self.assertTrue(check_names(self.broken("`read_org`"), self.names))

    def test_allowlist_is_not_a_loophole_for_json_names(self):
        self.assertFalse(NON_JSON_NAMES & (self.names["ids"] | self.names["details"]))

    def test_frontmatter(self):
        self.assertEqual([], check_frontmatter(self.text))
        no_name = re.sub(r"(?m)^name: .*\n", "", self.text, count=1)
        self.assertTrue(check_frontmatter(no_name))
        wrong = self.text.replace("name: bootstrap-guide", "name: other", 1)
        self.assertTrue(check_frontmatter(wrong))
        empty = re.sub(r"(?m)^description: .*$", "description:", self.text, count=1)
        self.assertTrue(check_frontmatter(empty))
        self.assertTrue(check_frontmatter(self.text.split("---", 2)[2]))

    def test_higher_version_rule(self):
        self.assertEqual([], check_stops_on_higher_version(self.text))
        self.assertTrue(check_stops_on_higher_version(self.text.replace("higher", "lower")))
        self.assertTrue(check_stops_on_higher_version(re.sub(r"(?i)stop", "go on", self.text)))
        self.assertTrue(check_stops_on_higher_version("no rule here"))

    def test_preview_exit_rule(self):
        self.assertEqual([], check_preview_exit_rule(self.text))
        self.assertTrue(check_preview_exit_rule(self.text.replace("other than 3", "of 3")))
        self.assertTrue(check_preview_exit_rule(self.text.replace("do not ask for a yes", "ask")))
        self.assertTrue(check_preview_exit_rule("no rule here"))

    def test_format_version_is_case_insensitive(self):
        self.assertEqual([], check_format_version("This skill knows Format Version 1."))
        self.assertTrue(check_format_version("This skill knows Format Version 2."))

    def test_missing_doc_subsection_is_a_readable_failure(self):
        doc = self.doc.replace("### Steps and details keys", "### Renamed")
        with self.assertRaisesRegex(AssertionError, "no subsection"):
            known_names(doc)

    def test_link_must_reach_the_doc_from_the_skill(self):
        self.assertEqual([], check_links(self.text, SKILL))
        self.assertTrue(check_links(self.broken("[x](docs/bootstrap.md#bootstrap-issue)"), SKILL))
        self.assertTrue(check_links(
            self.broken("[x](../../docs/bootstrap.md#bootstrap-issue)"), SKILL))
        # the anchor check still sees the anchor behind the relative prefix
        text = self.broken("[x](../../../docs/bootstrap.md#no-such-heading)")
        self.assertTrue(check_anchors(text, doc_anchors(self.doc)))

    def test_every_relative_link_is_resolved(self):
        self.assertTrue(check_links(self.broken("[y](../../../docs/nope.md#a)"), SKILL))
        self.assertTrue(check_links(self.broken("[y](../../../docs/nope.md)"), SKILL))
        # an existing file without an anchor passes
        self.assertEqual([], check_links(self.broken("[x](../../../docs/bootstrap.md)"), SKILL))
        # an anchor on another Markdown file is checked against that file's headings
        self.assertTrue(check_links(
            self.broken("[x](../../../docs/roadmap-format.md#no-such-heading)"), SKILL))
        # an in-page anchor is checked against the skill's own headings
        self.assertEqual([], check_links(self.broken("[x](#acting-on-a-step)"), SKILL))
        self.assertTrue(check_links(self.broken("[x](#no-such-heading)"), SKILL))
        # external links are not resolved
        self.assertEqual([], check_links(self.broken("[x](https://example.org/a#b)"), SKILL))


if __name__ == "__main__":
    unittest.main()
