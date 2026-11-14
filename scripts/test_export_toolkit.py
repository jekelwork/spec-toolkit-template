import contextlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import export_toolkit  # noqa: E402

SCRIPT = Path(__file__).with_name("export_toolkit.py")
MANIFEST = """\
include = ["tools/**", "README.md", "docs/*.md", "link"]
exclude = ["tools/private/**", "docs/handoff-*.md"]

[symlinks]
link = "tools/a.py"
"""


def clean_env():
    """Environment without GIT_* so a git hook cannot point children at the real repo."""
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
        env=clean_env(),
    )


class ExportCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.name", "Test")
        git(self.repo, "config", "user.email", "test@example.invalid")
        git(self.repo, "config", "commit.gpgsign", "false")
        self.manifest = self.tmp / "manifest.toml"
        self.manifest.write_text(MANIFEST, encoding="utf-8")

    def set_manifest(self, include, exclude=(), symlinks=None):
        text = f"include = {json.dumps(list(include))}\n"
        text += f"exclude = {json.dumps(list(exclude))}\n"
        if symlinks:
            text += "\n[symlinks]\n"
            for path, target in symlinks.items():
                text += f"{json.dumps(path)} = {json.dumps(target)}\n"
        self.manifest.write_text(text, encoding="utf-8")

    def write(self, path, content="x", mode=None):
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")
        if mode:
            target.chmod(mode)

    def link(self, path, target):
        p = self.repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(target, p)

    def commit(self, message="c"):
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)

    def standard(self):
        self.write("tools/a.py", b"print('a')\x00\xff")
        self.write("tools/run.sh", "#!/bin/sh\n", 0o755)
        self.write("tools/private/secret.txt", "s")
        self.write("README.md", "readme")
        self.write("docs/guide.md", "g")
        self.write("docs/handoff-1.md", "h")
        self.link("link", "tools/a.py")
        self.commit()

    def run_script(self, *args, manifest=True, cwd=None):
        cmd = [sys.executable, str(SCRIPT), "--repo", str(self.repo)]
        if manifest:
            cmd += ["--manifest", str(self.manifest)]
        return subprocess.run(
            [*cmd, *args],
            capture_output=True,
            text=True,
            check=False,
            env=clean_env(),
            cwd=cwd,
        )


class GlobTest(unittest.TestCase):
    def m(self, pattern, path):
        return export_toolkit.matches(pattern, path)

    def test_double_star_directory(self):
        self.assertTrue(self.m("specs/**", "specs/a"))
        self.assertTrue(self.m("specs/**", "specs/a/b/c.md"))
        self.assertFalse(self.m("specs/**", "specs"))
        self.assertFalse(self.m("specs/**", "other/specs/a"))

    def test_double_star_prefix_matches_any_depth(self):
        self.assertTrue(self.m("**/x.md", "x.md"))
        self.assertTrue(self.m("**/x.md", "a/b/x.md"))
        self.assertFalse(self.m("**/x.md", "a/y.md"))

    def test_single_star_stays_in_one_segment(self):
        self.assertTrue(self.m("docs/handoff-*.md", "docs/handoff-x.md"))
        self.assertFalse(self.m("docs/*.md", "docs/a/b.md"))
        self.assertTrue(self.m(".github/workflows/*.yml", ".github/workflows/a.yml"))

    def test_patterns_are_anchored_and_literal_dots(self):
        self.assertFalse(self.m("README.md", "docs/README.md"))
        self.assertFalse(self.m("README.md", "READMExmd"))
        self.assertTrue(self.m("[ab].txt", "a.txt"))
        self.assertTrue(self.m("a?.txt", "ab.txt"))
        self.assertFalse(self.m("a?.txt", "a/.txt"))


class ClassifyTest(unittest.TestCase):
    def test_exclude_wins_and_unclassified_reported(self):
        included, excluded, unclassified = export_toolkit.classify(
            ["tools/a", "tools/private/b", "z/one", "y"],
            ["tools/**"],
            ["tools/private/**"],
        )
        self.assertEqual(included, ["tools/a"])
        self.assertEqual(excluded, ["tools/private/b"])
        self.assertEqual(unclassified, ["y", "z/one"])


class CheckTest(ExportCase):
    def test_check_passes(self):
        self.standard()
        r = self.run_script("--check")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_check_lists_every_unclassified_path(self):
        self.standard()
        self.write("new1.txt")
        self.write("deep/new2.txt")
        self.commit()
        r = self.run_script("--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("new1.txt", r.stderr)
        self.assertIn("deep/new2.txt", r.stderr)
        self.assertNotIn("tools/a.py", r.stderr)

    def test_check_writes_nothing(self):
        self.standard()
        before = sorted(p.name for p in self.tmp.iterdir())
        self.run_script("--check")
        self.assertEqual(before, sorted(p.name for p in self.tmp.iterdir()))

    def test_modes_are_mutually_exclusive_and_required(self):
        self.standard()
        self.assertNotEqual(self.run_script().returncode, 0)
        self.assertNotEqual(self.run_script("--check", "--list").returncode, 0)

    def test_missing_manifest_fails(self):
        self.standard()
        self.manifest.unlink()
        r = self.run_script("--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("manifest", r.stderr.lower())

    def test_submodule_is_an_error(self):
        self.standard()
        git(
            self.repo,
            "update-index",
            "--add",
            "--cacheinfo",
            "160000," + "1" * 40 + ",tools/sub",
        )
        git(self.repo, "commit", "-q", "-m", "sub")
        r = self.run_script("--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("submodule", r.stderr.lower())
        self.assertIn("tools/sub", r.stderr)


class ListTest(ExportCase):
    def test_list_sorted_included_only(self):
        self.standard()
        r = self.run_script("--list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            r.stdout.splitlines(),
            ["README.md", "docs/guide.md", "link", "tools/a.py", "tools/run.sh"],
        )

    def test_list_refuses_unclassified(self):
        self.standard()
        self.write("stray")
        self.commit()
        r = self.run_script("--list")
        self.assertEqual(r.returncode, 1)
        self.assertIn("stray", r.stderr)


class OutputTest(ExportCase):
    def test_output_writes_exact_files(self):
        self.standard()
        out = self.tmp / "out"
        r = self.run_script("--output", str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        found = sorted(
            str(p.relative_to(out))
            for p in out.rglob("*")
            if p.is_file() or p.is_symlink()
        )
        self.assertEqual(
            found,
            ["README.md", "docs/guide.md", "link", "tools/a.py", "tools/run.sh"],
        )
        self.assertEqual((out / "tools/a.py").read_bytes(), b"print('a')\x00\xff")
        self.assertTrue(os.stat(out / "tools/run.sh").st_mode & stat.S_IXUSR)
        self.assertFalse(os.stat(out / "README.md").st_mode & stat.S_IXUSR)
        self.assertTrue((out / "link").is_symlink())
        self.assertEqual(os.readlink(out / "link"), "tools/a.py")
        self.assertIn("5", r.stdout)

    def test_output_into_existing_empty_dir_ok(self):
        self.standard()
        out = self.tmp / "out"
        out.mkdir()
        self.assertEqual(self.run_script("--output", str(out)).returncode, 0)

    def test_output_refuses_non_empty_dir(self):
        self.standard()
        out = self.tmp / "out"
        out.mkdir()
        (out / "keep").write_text("k")
        r = self.run_script("--output", str(out))
        self.assertEqual(r.returncode, 1)
        self.assertIn("empty", r.stderr)
        self.assertEqual([p.name for p in out.iterdir()], ["keep"])

    def test_output_refuses_existing_file(self):
        self.standard()
        out = self.tmp / "out"
        out.write_text("f")
        self.assertEqual(self.run_script("--output", str(out)).returncode, 1)

    def test_output_refuses_inside_repo(self):
        self.standard()
        out = self.repo / "export"
        r = self.run_script("--output", str(out))
        self.assertEqual(r.returncode, 1)
        self.assertIn("inside", r.stderr)
        self.assertFalse(out.exists())

    def test_output_refuses_unclassified(self):
        self.standard()
        self.write("stray")
        self.commit()
        out = self.tmp / "out"
        r = self.run_script("--output", str(out))
        self.assertEqual(r.returncode, 1)
        self.assertIn("stray", r.stderr)
        self.assertFalse(out.exists())


class TarballTest(ExportCase):
    def test_tarball_layout_and_modes(self):
        self.standard()
        tb = self.tmp / "my-toolkit.tar.gz"
        r = self.run_script("--tarball", str(tb))
        self.assertEqual(r.returncode, 0, r.stderr)
        with tarfile.open(tb, "r:gz") as t:
            members = {m.name: m for m in t.getmembers()}
            files = sorted(n for n, m in members.items() if not m.isdir())
            self.assertEqual(
                files,
                [
                    "my-toolkit/README.md",
                    "my-toolkit/docs/guide.md",
                    "my-toolkit/link",
                    "my-toolkit/tools/a.py",
                    "my-toolkit/tools/run.sh",
                ],
            )
            self.assertTrue(all(n.split("/")[0] == "my-toolkit" for n in members))
            self.assertEqual(members["my-toolkit/tools/run.sh"].mode & 0o777, 0o755)
            self.assertEqual(members["my-toolkit/README.md"].mode & 0o777, 0o644)
            self.assertTrue(members["my-toolkit/link"].issym())
            self.assertEqual(members["my-toolkit/link"].linkname, "tools/a.py")
            self.assertEqual(
                t.extractfile("my-toolkit/tools/a.py").read(), b"print('a')\x00\xff"
            )

    def test_tarball_refuses_existing_inside_repo_and_bad_suffix(self):
        self.standard()
        tb = self.tmp / "x.tar.gz"
        tb.write_text("old")
        self.assertEqual(self.run_script("--tarball", str(tb)).returncode, 1)
        self.assertEqual(tb.read_text(), "old")
        inside = self.repo / "x.tar.gz"
        r = self.run_script("--tarball", str(inside))
        self.assertEqual(r.returncode, 1)
        self.assertIn("inside", r.stderr)
        self.assertFalse(inside.exists())
        self.assertEqual(
            self.run_script("--tarball", str(self.tmp / "x.zip")).returncode, 1
        )


STD_INCLUDE = ["tools/**", "README.md", "docs/*.md", "link"]
STD_EXCLUDE = ["tools/private/**", "docs/handoff-*.md"]


class SymlinkTest(ExportCase):
    def declare_target(self, target):
        """Commit link -> target and declare exactly that target in the manifest."""
        self.standard()
        (self.repo / "link").unlink()
        self.link("link", target)
        self.commit("link target")
        self.set_manifest(STD_INCLUDE, STD_EXCLUDE, {"link": target})
        return self.run_script("--list")

    def test_target_mismatch_refused(self):
        self.standard()
        (self.repo / "link").unlink()
        self.link("link", "tools/run.sh")
        self.commit("other target")
        r = self.run_script("--list")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("target mismatch", r.stderr)
        self.assertIn("link", r.stderr)

    def test_absolute_declared_target_refused_by_manifest_check(self):
        r = self.declare_target("/etc/passwd")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("manifest", r.stderr)
        self.assertIn("unsafe", r.stderr)

    def test_escaping_declared_target_refused(self):
        r = self.declare_target("../outside")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("escapes the exported tree", r.stderr)

    def test_excluded_declared_target_refused(self):
        r = self.declare_target("tools/private/secret.txt")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("not part of the export", r.stderr)

    def test_unsafe_manifest_symlink_entries_refused(self):
        self.standard()
        for symlinks in (
            {"link": "/abs"},
            {"link": ""},
            {"link": "a\nb"},
            {"../x": "tools/a.py"},
            {"tools/.git/x": "tools/a.py"},
        ):
            self.set_manifest(STD_INCLUDE, STD_EXCLUDE, symlinks)
            r = self.run_script("--check")
            self.assertEqual(r.returncode, 1, symlinks)
            self.assertIn("manifest", r.stderr, symlinks)
            self.assertIn("unsafe", r.stderr, symlinks)
            self.assertNotIn("Traceback", r.stderr)

    def test_directory_target_with_exported_files_ok(self):
        self.standard()
        (self.repo / "link").unlink()
        self.link("link", "tools")
        self.commit("dir link")
        self.set_manifest(STD_INCLUDE, STD_EXCLUDE, {"link": "tools"})
        self.assertEqual(self.run_script("--list").returncode, 0)

    def test_undeclared_symlink_refused(self):
        self.standard()
        self.set_manifest(STD_INCLUDE, STD_EXCLUDE)
        r = self.run_script("--list")
        self.assertEqual(r.returncode, 1)
        self.assertIn("undeclared symlink", r.stderr)
        self.assertIn("link", r.stderr)
        self.assertIn("tools/a.py", r.stderr)

    def test_declared_symlink_missing_at_ref_refused(self):
        self.standard()
        self.set_manifest(
            STD_INCLUDE,
            STD_EXCLUDE,
            {"link": "tools/a.py", "ghost": "tools/a.py"},
        )
        r = self.run_script("--list")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ghost", r.stderr)

    def test_declared_path_that_is_a_regular_file_refused(self):
        self.standard()
        self.set_manifest(
            STD_INCLUDE,
            STD_EXCLUDE,
            {"link": "tools/a.py", "README.md": "tools/a.py"},
        )
        r = self.run_script("--list")
        self.assertEqual(r.returncode, 1)
        self.assertIn("README.md", r.stderr)
        self.assertIn("not a symlink", r.stderr)

    def test_declared_target_must_resolve_to_exported_path(self):
        self.standard()
        (self.repo / "link").unlink()
        self.link("link", "tools/private/secret.txt")
        self.commit("excluded target")
        self.set_manifest(
            STD_INCLUDE,
            STD_EXCLUDE,
            {"link": "tools/private/secret.txt"},
        )
        r = self.run_script("--list")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not part of the export", r.stderr)

    def test_declared_symlink_must_be_exported(self):
        self.standard()
        self.set_manifest(
            ["tools/**", "README.md", "docs/*.md"],
            [*STD_EXCLUDE, "link"],
            {"link": "tools/a.py"},
        )
        r = self.run_script("--list")
        self.assertEqual(r.returncode, 1)
        self.assertIn("link", r.stderr)


class RefTest(ExportCase):
    def test_uncommitted_edit_not_exported_and_warned(self):
        self.standard()
        self.write("README.md", "dirty")
        self.write("tools/new.py", "untracked")
        out = self.tmp / "out"
        r = self.run_script("--output", str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((out / "README.md").read_text(), "readme")
        self.assertFalse((out / "tools/new.py").exists())
        self.assertIn("warning", r.stderr.lower())
        self.assertIn("README.md", r.stderr)
        self.assertIn("tools/new.py", r.stderr)

    def test_repository_fsmonitor_hook_is_not_executed(self):
        self.standard()
        marker = self.tmp / "fsmonitor-ran"
        hook = self.tmp / "hook.sh"
        hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        hook.chmod(0o755)
        git(self.repo, "config", "core.fsmonitor", str(hook))
        self.write("README.md", "dirty")
        r = self.run_script("--list")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("README.md", r.stderr)  # status still ran and warned
        self.assertFalse(marker.exists(), "core.fsmonitor hook was executed")

    def test_no_warning_when_clean(self):
        self.standard()
        r = self.run_script("--list")
        self.assertNotIn("warning", r.stderr.lower())

    def test_uncommitted_excluded_change_not_warned(self):
        self.standard()
        self.write("tools/private/secret.txt", "changed")
        r = self.run_script("--list")
        self.assertNotIn("warning", r.stderr.lower())

    def test_ref_selects_older_commit(self):
        self.standard()
        first = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.write("tools/later.py", "later")
        self.write("README.md", "v2")
        self.commit("later")
        out = self.tmp / "out"
        r = self.run_script("--ref", first, "--output", str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((out / "tools/later.py").exists())
        self.assertEqual((out / "README.md").read_text(), "readme")

    def test_bad_ref_fails(self):
        self.standard()
        r = self.run_script("--ref", "nope", "--list")
        self.assertEqual(r.returncode, 1)
        self.assertIn("nope", r.stderr)


class CraftedTreeCase(ExportCase):
    """Trees built with git plumbing that a normal checkout cannot create."""

    def hash_blob(self, data):
        if isinstance(data, str):
            data = data.encode()
        result = subprocess.run(
            ["git", "-C", str(self.repo), "hash-object", "-w", "--stdin"],
            input=data,
            capture_output=True,
            check=True,
            env=clean_env(),
        )
        return result.stdout.decode().strip()

    def mktree(self, spec):
        lines = []
        for name, value in spec.items():
            if isinstance(value, dict):
                lines.append(f"040000 tree {self.mktree(value)}\t{name}")
            elif isinstance(value, tuple) and value[0] == "link":
                lines.append(f"120000 blob {self.hash_blob(value[1])}\t{name}")
            else:
                lines.append(f"100644 blob {self.hash_blob(value)}\t{name}")
        result = subprocess.run(
            ["git", "-C", str(self.repo), "mktree"],
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
            check=True,
            env=clean_env(),
        )
        return result.stdout.strip()

    def crafted_commit(self, spec):
        tree = self.mktree(spec)
        return git(self.repo, "commit-tree", tree, "-m", "crafted").stdout.strip()


class UnsafePathTest(CraftedTreeCase):
    def test_dotdot_component_refused_and_nothing_written_outside(self):
        ref = self.crafted_commit(
            {"tools": {"..": {"..": {"pwned": "x"}}}, "README.md": "r"}
        )
        dest = self.tmp / "dest" / "inner"
        for mode in (["--check"], ["--output", str(dest)]):
            r = self.run_script("--ref", ref, *mode)
            self.assertEqual(r.returncode, 1, r.stdout)
            self.assertIn("unsafe", r.stderr)
        tb = self.tmp / "dest" / "t.tar.gz"
        r = self.run_script("--ref", ref, "--tarball", str(tb))
        self.assertEqual(r.returncode, 1)
        self.assertFalse(tb.exists())
        self.assertFalse((self.tmp / "dest").exists())
        self.assertFalse((self.tmp / "pwned").exists())

    def test_dot_git_and_backslash_components_refused(self):
        for spec in (
            {"tools": {".GIT": {"hooks": "x"}}},
            {"tools": {"a\\b": "x"}},
        ):
            ref = self.crafted_commit(spec)
            r = self.run_script("--ref", ref, "--check")
            self.assertEqual(r.returncode, 1, spec)
            self.assertIn("unsafe", r.stderr)


class SymlinkChainTest(CraftedTreeCase):
    def test_chain_through_another_declared_symlink_refused(self):
        self.set_manifest(
            ["tools/**", "L", "x/**"],
            symlinks={
                "tools/deep/up": "../../x",
                "L": "tools/deep/up/../../../x/f",
            },
        )
        ref = self.crafted_commit(
            {
                "tools": {"deep": {"up": ("link", "../../x")}},
                "L": ("link", "tools/deep/up/../../../x/f"),
                "x": {"f": "1"},
            }
        )
        r = self.run_script("--ref", ref, "--list")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("passes through another symlink", r.stderr)

    def test_declared_link_to_declared_link_refused(self):
        self.set_manifest(
            ["tools/**", "L"], symlinks={"tools/b": "a", "L": "tools/b"}
        )
        ref = self.crafted_commit(
            {"tools": {"a": "1", "b": ("link", "a")}, "L": ("link", "tools/b")}
        )
        r = self.run_script("--ref", ref, "--list")
        self.assertEqual(r.returncode, 1)
        self.assertIn("passes through another symlink", r.stderr)

    def test_undeclared_link_to_link_refused(self):
        self.set_manifest(["tools/**", "L"], symlinks={"tools/b": "a"})
        ref = self.crafted_commit(
            {"tools": {"a": "1", "b": ("link", "a")}, "L": ("link", "tools/b")}
        )
        r = self.run_script("--ref", ref, "--list")
        self.assertEqual(r.returncode, 1)
        self.assertIn("undeclared symlink", r.stderr)

    def test_path_below_declared_symlink_refused(self):
        # tools/** is exported, so the target exists and only the
        # "lies below" rule can fire.
        entries = {
            "S": ("120000", self.hash_blob("tools")),
            "S/f": ("100644", self.hash_blob("x")),
            "tools/a": ("100644", self.hash_blob("y")),
        }
        self.standard()
        with self.assertRaisesRegex(export_toolkit.ExportError, "lies below"):
            export_toolkit.validate_symlinks(
                self.repo, entries, ["S", "S/f", "tools/a"], {"S": "tools"}
            )

    def test_resolve_lexically(self):
        resolve = export_toolkit.resolve_lexically
        self.assertEqual(resolve("a/b/link", "../../x/f", set()), "x/f")
        self.assertEqual(resolve("a/link", "b/c/../d", set()), "a/b/d")
        self.assertEqual(resolve("a/link", "./b//c", set()), "a/b/c")
        self.assertEqual(
            resolve(".claude/skills", "../.agents/skills", set()), ".agents/skills"
        )
        self.assertEqual(resolve("a/link", "..", set()), "")

    def test_resolve_lexically_refuses_escape_cleanly(self):
        resolve = export_toolkit.resolve_lexically
        for path, target in (("link", "../x"), ("a/link", "../../x"), ("link", "..")):
            with self.assertRaisesRegex(export_toolkit.ExportError, "escapes"):
                resolve(path, target, set())

    def test_resolve_lexically_refuses_declared_links_on_the_way(self):
        resolve = export_toolkit.resolve_lexically
        with self.assertRaisesRegex(export_toolkit.ExportError, "passes through"):
            resolve("L", "tools/b/c", {"tools/b"})
        with self.assertRaisesRegex(export_toolkit.ExportError, "passes through"):
            resolve("L", "tools/b", {"tools/b"})
        with self.assertRaisesRegex(export_toolkit.ExportError, "passes through"):
            resolve("tools/L", "b/c", {"tools/b"})

    def test_case_variant_chain_refused(self):
        self.set_manifest(
            ["tools/**", "L", "x/**"], symlinks={"tools/deep/up": "../../x"}
        )
        ref = self.crafted_commit(
            {
                "tools": {"deep": {"up": ("link", "../../x")}},
                "L": ("link", "TOOLS/DEEP/UP/../../../x/f"),
                "x": {"f": "1"},
            }
        )
        r = self.run_script("--ref", ref, "--list")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("undeclared symlink", r.stderr)

    def test_nul_byte_in_undeclared_link_refused(self):
        self.set_manifest(["tools/**", "L"])
        ref = self.crafted_commit({"tools": {"a": "1"}, "L": ("link", "tools/a\0x")})
        r = self.run_script("--ref", ref, "--list")
        self.assertEqual(r.returncode, 1)
        self.assertIn("undeclared symlink", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_written_symlink_resolving_outside_destination_fails(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        entries = {"ln": ("120000", self.hash_blob("../outside"))}
        dest = self.tmp / "dest"
        with self.assertRaises(export_toolkit.ExportError):
            export_toolkit.write_tree(self.repo, entries, ["ln"], dest)

    def test_write_refuses_to_go_through_a_just_written_symlink(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        entries = {
            "d": ("120000", self.hash_blob(str(outside))),
            "d/f": ("100644", self.hash_blob("evil")),
        }
        with self.assertRaisesRegex(
            export_toolkit.ExportError, "refusing to write through the symlink"
        ):
            export_toolkit.write_tree(
                self.repo, entries, ["d", "d/f"], self.tmp / "dest"
            )
        self.assertEqual(list(outside.iterdir()), [])

    def test_write_containment_check_catches_parent_outside_destination(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        entries = {
            "d": ("120000", self.hash_blob(str(outside))),
            "d/f": ("100644", self.hash_blob("evil")),
        }
        # Disable the earlier symlink refusal so only the containment check is left.
        with mock.patch.object(export_toolkit.Path, "is_symlink", return_value=False):
            with self.assertRaisesRegex(export_toolkit.ExportError, "outside"):
                export_toolkit.write_tree(
                    self.repo, entries, ["d", "d/f"], self.tmp / "dest"
                )
        self.assertEqual(list(outside.iterdir()), [])

    def test_written_symlink_inside_destination_ok(self):
        entries = {
            "d/f": ("100644", self.hash_blob("x")),
            "ln": ("120000", self.hash_blob("d")),
        }
        dest = self.tmp / "dest"
        export_toolkit.write_tree(self.repo, entries, ["d/f", "ln"], dest)
        self.assertEqual((dest / "ln" / "f").read_text(), "x")

    def test_directory_symlink_to_exported_dir_still_works(self):
        self.set_manifest(
            [".agents/**", ".claude/skills"],
            symlinks={".claude/skills": "../.agents/skills"},
        )
        ref = self.crafted_commit(
            {
                ".agents": {"skills": {"a": {"SKILL.md": "s"}}},
                ".claude": {"skills": ("link", "../.agents/skills")},
            }
        )
        r = self.run_script("--ref", ref, "--list")
        self.assertEqual(r.returncode, 0, r.stderr)


class CollisionTest(CraftedTreeCase):
    def test_casefold_collision_refused_listing_both(self):
        ref = self.crafted_commit({"tools": {"Dup": "1", "dup": "2"}})
        r = self.run_script("--ref", ref, "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("tools/Dup", r.stderr)
        self.assertIn("tools/dup", r.stderr)

    def test_ignorable_code_points_collide(self):
        ref = self.crafted_commit({"tools": {"a\u200cb": "1", "ab": "2"}})
        r = self.run_script("--ref", ref, "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("collide", r.stderr)
        ref = self.crafted_commit(
            {".claude\u200c": {"skills": {"x": "1"}}, ".claude": {"skills": {"y": "2"}}}
        )
        self.set_manifest([".claude*/**"])
        r = self.run_script("--ref", ref, "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("collide", r.stderr)

    def test_file_versus_directory_collision_refused(self):
        ref = self.crafted_commit({"tools": {"X": "1", "x": {"f": "2"}}})
        r = self.run_script("--ref", ref, "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("tools/X", r.stderr)

    def test_symlink_then_differently_cased_directory_refused(self):
        self.set_manifest(["0/**", "A", "a/**"])
        ref = self.crafted_commit(
            {"0": {"f": "zero"}, "A": ("link", "0"), "a": {"f": "alpha"}}
        )
        out = self.tmp / "out"
        r = self.run_script("--ref", ref, "--output", str(out))
        self.assertEqual(r.returncode, 1)
        self.assertIn("collide", r.stderr)
        self.assertFalse(out.exists())

    def test_write_refuses_parent_resolving_outside_destination(self):
        outside = self.tmp / "outside"
        outside.mkdir()
        entries = {
            "ln": ("120000", self.hash_blob(str(outside))),
            "ln/f": ("100644", self.hash_blob("evil")),
        }
        dest = self.tmp / "dest"
        with self.assertRaises(export_toolkit.ExportError):
            export_toolkit.write_tree(self.repo, entries, ["ln", "ln/f"], dest)
        self.assertEqual(list(outside.iterdir()), [])


class ManifestFromRefTest(ExportCase):
    def test_default_manifest_comes_from_ref_not_working_tree(self):
        self.write(
            "scripts/toolkit-export.toml",
            'include = ["README.md", "scripts/**"]\nexclude = []\n',
        )
        self.write("README.md", "r")
        self.commit()
        self.write(
            "scripts/toolkit-export.toml", 'include = ["README.md"]\nexclude = []\n'
        )
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(self.repo), "--list"],
            capture_output=True,
            text=True,
            env=clean_env(),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(
            r.stdout.splitlines(), ["README.md", "scripts/toolkit-export.toml"]
        )

    def test_explicit_manifest_overrides(self):
        self.standard()
        r = self.run_script("--list")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_missing_manifest_at_ref_fails(self):
        self.write("README.md", "r")
        self.commit()
        r = self.run_script("--list", manifest=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("manifest", r.stderr.lower())


class InsideRepoTest(ExportCase):
    def test_nested_nonexistent_path_refused(self):
        self.standard()
        out = self.repo / "a" / "b" / "out"
        r = self.run_script("--output", str(out))
        self.assertEqual(r.returncode, 1)
        self.assertIn("inside", r.stderr)
        self.assertFalse((self.repo / "a").exists())

    def test_differently_cased_path_refused_on_case_insensitive_fs(self):
        self.standard()
        variant = self.repo.parent / self.repo.name.upper()
        if not variant.exists():
            self.skipTest("case-sensitive filesystem")
        out = variant / "caseout"
        r = self.run_script("--output", str(out))
        self.assertEqual(r.returncode, 1)
        self.assertIn("inside", r.stderr)
        self.assertFalse((self.repo / "caseout").exists())

    def test_alias_to_subdirectory_refused(self):
        self.standard()
        alias = self.tmp / "alias"
        os.symlink(self.repo / "tools", alias)
        for args in (
            ["--output", str(alias / "out")],
            ["--output", f"{alias}/../out2"],
            ["--tarball", str(alias / "t.tar.gz")],
            ["--tarball", f"{alias}/../t2.tar.gz"],
        ):
            r = self.run_script(*args)
            self.assertEqual(r.returncode, 1, args)
            self.assertIn("inside", r.stderr)
        self.assertEqual(
            sorted(p.name for p in self.repo.iterdir() if p.name != ".git"),
            ["README.md", "docs", "link", "tools"],
        )
        self.assertFalse((self.repo / "tools" / "out").exists())
        self.assertFalse((self.repo / "tools" / "t.tar.gz").exists())

    def test_dangling_symlink_on_the_way_refused(self):
        self.standard()
        dl = self.tmp / "dl"
        os.symlink(self.repo / "newdir", dl)
        for args in (
            ["--output", str(dl / "x")],
            ["--tarball", str(dl / "t.tar.gz")],
        ):
            r = self.run_script(*args)
            self.assertEqual(r.returncode, 1, args)
            self.assertIn("dangling", r.stderr)
            self.assertNotIn("Traceback", r.stderr)
        self.assertFalse((self.repo / "newdir").exists())

    def test_symlinked_path_into_repo_refused(self):
        self.standard()
        alias = self.tmp / "alias"
        os.symlink(self.repo, alias)
        r = self.run_script("--output", str(alias / "out"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("inside", r.stderr)


class GlobNitTest(unittest.TestCase):
    def m(self, pattern, path):
        return export_toolkit.matches(pattern, path)

    def test_star_matches_newline_in_names(self):
        self.assertTrue(self.m("tools/*", "tools/a\nb"))
        self.assertTrue(self.m("tools/**", "tools/a\nb/c"))

    def test_bracket_classes_never_match_slash(self):
        self.assertFalse(self.m("a[!x]b", "a/b"))
        self.assertFalse(self.m("a[/]b", "a/b"))
        self.assertTrue(self.m("a[!x]b", "ayb"))
        self.assertFalse(self.m("a[!x]b", "axb"))

    def test_leading_caret_in_class_is_literal(self):
        self.assertTrue(self.m("[^x].txt", "^.txt"))
        self.assertTrue(self.m("[^x].txt", "x.txt"))
        self.assertFalse(self.m("[^x].txt", "a.txt"))

    def test_bang_negates_and_leading_close_bracket_is_literal(self):
        self.assertTrue(self.m("[!a]", "b"))
        self.assertFalse(self.m("[!a]", "a"))
        self.assertTrue(self.m("[]a]", "]"))


class ManifestValidationTest(ExportCase):
    def test_ill_formed_manifests_fail_cleanly(self):
        self.standard()
        for text in (
            "include = [",
            'include = "tools/**"\nexclude = []\n',
            'include = ["a", 1]\nexclude = []\n',
            'include = ["a"]\n',
        ):
            self.manifest.write_text(text, encoding="utf-8")
            r = self.run_script("--check")
            self.assertEqual(r.returncode, 1, text)
            self.assertIn("manifest", r.stderr.lower())
            self.assertNotIn("Traceback", r.stderr)


class RobustnessTest(CraftedTreeCase):
    def test_non_utf8_path_is_a_clear_error(self):
        inner = subprocess.run(
            ["git", "-C", str(self.repo), "mktree"],
            input=b"100644 blob " + self.hash_blob("x").encode() + b"\t\xff.txt\n",
            capture_output=True,
            check=True,
            env=clean_env(),
        ).stdout.strip()
        outer = subprocess.run(
            ["git", "-C", str(self.repo), "mktree"],
            input=b"040000 tree " + inner + b"\ttools\n",
            capture_output=True,
            check=True,
            env=clean_env(),
        ).stdout.strip()
        ref = git(self.repo, "commit-tree", outer.decode(), "-m", "x").stdout.strip()
        r = self.run_script("--ref", ref, "--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("UTF-8", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_ref_starting_with_dash_is_not_an_option(self):
        self.standard()
        r = self.run_script("--ref=--output=x", "--list", cwd=self.tmp)
        self.assertEqual(r.returncode, 1)
        self.assertIn("--output=x", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse((self.tmp / "x").exists())

    def test_bad_tarball_names_refused(self):
        self.standard()
        for name in ("..tar.gz", "...tar.gz", "a\\b.tar.gz", ".tar.gz"):
            r = self.run_script("--tarball", str(self.tmp / name))
            self.assertEqual(r.returncode, 1, name)
            self.assertNotIn("Traceback", r.stderr)
            self.assertFalse((self.tmp / name).exists(), name)

    def test_tarballs_are_byte_identical(self):
        self.standard()
        (self.tmp / "one").mkdir()
        (self.tmp / "two").mkdir()
        a, b = self.tmp / "one" / "a.tar.gz", self.tmp / "two" / "a.tar.gz"
        self.assertEqual(self.run_script("--tarball", str(a)).returncode, 0)
        self.assertEqual(self.run_script("--tarball", str(b)).returncode, 0)
        self.assertEqual(a.read_bytes(), b.read_bytes())

    def test_output_that_is_a_broken_symlink_refused(self):
        self.standard()
        out = self.tmp / "brk"
        os.symlink(self.tmp / "missing", out)
        r = self.run_script("--output", str(out))
        self.assertEqual(r.returncode, 1)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse((self.tmp / "missing").exists())

    def run_main(self, *args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = export_toolkit.main(
                ["--repo", str(self.repo), "--manifest", str(self.manifest), *args]
            )
        return code, err.getvalue()

    def leftovers(self):
        return sorted(p.name for p in self.tmp.iterdir())

    def test_failed_output_leaves_nothing_behind(self):
        self.standard()
        before = self.leftovers()
        out = self.tmp / "out"
        with mock.patch.object(export_toolkit.os, "symlink", side_effect=OSError("boom")):
            code, err = self.run_main("--output", str(out))
        self.assertEqual(code, 1)
        self.assertIn("error:", err)
        self.assertIn("boom", err)
        self.assertEqual(self.leftovers(), before)

    def test_failed_output_keeps_existing_empty_dir_empty(self):
        self.standard()
        out = self.tmp / "out"
        out.mkdir()
        before = self.leftovers()
        with mock.patch.object(export_toolkit.os, "symlink", side_effect=OSError("boom")):
            code, _ = self.run_main("--output", str(out))
        self.assertEqual(code, 1)
        self.assertEqual(list(out.iterdir()), [])
        self.assertEqual(self.leftovers(), before)

    def test_failed_tarball_leaves_nothing_behind(self):
        self.standard()
        before = self.leftovers()
        with mock.patch.object(
            export_toolkit.tarfile, "open", side_effect=OSError("disk full")
        ):
            code, err = self.run_main("--tarball", str(self.tmp / "t.tar.gz"))
        self.assertEqual(code, 1)
        self.assertIn("disk full", err)
        self.assertEqual(self.leftovers(), before)


class WarningTest(ExportCase):
    def test_staged_rename_is_reported_once(self):
        self.standard()
        git(self.repo, "mv", "docs/guide.md", "docs/guide2.md")
        for args in (["--list"], ["--output", str(self.tmp / "o")]):
            r = self.run_script(*args)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(r.stderr.lower().count("warning"), 1, r.stderr)
            self.assertIn("docs/guide2.md", r.stderr)
            self.assertIn("docs/guide.md", r.stderr)

    def test_tarball_warns_once(self):
        self.standard()
        self.write("README.md", "dirty")
        r = self.run_script("--tarball", str(self.tmp / "w.tar.gz"))
        self.assertEqual(r.stderr.lower().count("warning"), 1, r.stderr)


class PatternRobustnessTest(ExportCase):
    def match_in_subprocess(self, pattern, path, timeout=5):
        code = (
            "import sys; sys.path.insert(0, sys.argv[1]); import export_toolkit as e; "
            "sys.exit(0 if e.matches(sys.argv[2], sys.argv[3]) is False else 3)"
        )
        try:
            result = subprocess.run(
                [sys.executable, "-c", code, str(SCRIPT.parent), pattern, path],
                capture_output=True,
                timeout=timeout,
                env=clean_env(),
            )
        except subprocess.TimeoutExpired:
            self.fail(f"matching {pattern[:40]!r} did not finish in {timeout}s")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_redos_pattern_completes_quickly(self):
        self.match_in_subprocess("**a**a**a**a**a**a**a**a**a**a**b", "a" * 70)
        self.match_in_subprocess("*a*a*a*a*a*a*a*a*a*a*a*b", "a" * 70)
        self.match_in_subprocess("**/*a*a*a*a*a*a*a*a*b/**/**/**/x", "a/" * 40)

    def test_many_recursive_segments_match_in_polynomial_time(self):
        self.match_in_subprocess("/".join(["**", "a"] * 80) + "/x", "a/" * 300 + "y")
        self.match_in_subprocess("/".join(["**"] * 170) + "/x", "a/" * 300 + "y")

    def test_consecutive_double_stars_collapse(self):
        self.assertEqual(
            export_toolkit.compile_pattern("a/**/**/**/b"),
            export_toolkit.compile_pattern("a/**/b"),
        )
        self.assertTrue(export_toolkit.matches("a/**/**/b", "a/b"))
        self.assertTrue(export_toolkit.matches("a/**/**", "a/x/y"))
        self.assertFalse(export_toolkit.matches("a/**/**", "a"))

    def test_empty_pattern_segments_refused(self):
        self.standard()
        for pattern in ("dir/", "/dir", "a//b", ""):
            self.set_manifest([pattern, "tools/**"])
            r = self.run_script("--check")
            self.assertEqual(r.returncode, 1, pattern)
            self.assertIn("empty path segment", r.stderr)
            self.assertNotIn("Traceback", r.stderr)

    def test_dotfiles_are_not_special_for_wildcards(self):
        self.assertTrue(export_toolkit.matches("*", ".hidden"))
        self.assertTrue(export_toolkit.matches("tools/**", "tools/.x/.y"))

    def test_double_star_inside_a_segment_acts_like_single_star(self):
        self.assertTrue(export_toolkit.matches("x**y", "xzzy"))
        self.assertFalse(export_toolkit.matches("x**y", "x/y"))
        self.assertTrue(export_toolkit.matches("a/**/b", "a/b"))
        self.assertTrue(export_toolkit.matches("a/**/b", "a/x/y/b"))

    def test_invalid_patterns_give_a_clean_manifest_error(self):
        self.standard()
        for pattern in ("[z-a]", "a[b", "[!]", "x" * 600):
            self.set_manifest([pattern, "tools/**"])
            r = self.run_script("--check")
            self.assertEqual(r.returncode, 1, pattern)
            self.assertIn("include", r.stderr)
            self.assertNotIn("Traceback", r.stderr)
        self.set_manifest(["[z-a]"])
        r = self.run_script("--check")
        self.assertIn("[z-a]", r.stderr)

    def test_too_many_patterns_refused(self):
        self.standard()
        self.set_manifest([f"p{i}" for i in range(1100)])
        r = self.run_script("--check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("include", r.stderr)


class PathHardeningTest(CraftedTreeCase):
    def raw_commit(self, name_bytes):
        sha = self.hash_blob("x").encode()
        inner = subprocess.run(
            ["git", "-C", str(self.repo), "mktree", "-z"],
            input=b"100644 blob " + sha + b"\t" + name_bytes + b"\0",
            capture_output=True,
            check=True,
            env=clean_env(),
        ).stdout.strip()
        outer = subprocess.run(
            ["git", "-C", str(self.repo), "mktree"],
            input=b"040000 tree " + inner + b"\ttools\n",
            capture_output=True,
            check=True,
            env=clean_env(),
        ).stdout.strip()
        return git(self.repo, "commit-tree", outer.decode(), "-m", "x").stdout.strip()

    def test_control_characters_in_paths_refused(self):
        for name in ("a\nb", "a\x1bb", "a\x7fb", "a\u0085b", "a\tb"):
            ref = self.raw_commit(name.encode())
            r = self.run_script("--ref", ref, "--check")
            self.assertEqual(r.returncode, 1, repr(name))
            self.assertIn("unsafe", r.stderr)
            self.assertNotIn("Traceback", r.stderr)
            self.assertNotIn(name, r.stderr)

    def test_dot_git_with_ignorable_code_points_refused(self):
        for name in (".g\u200cit", ".gi\u200dt", "\ufeff.git", ".git\u2060", ".g\ufe0fit"):
            ref = self.raw_commit(name.encode())
            r = self.run_script("--ref", ref, "--check")
            self.assertEqual(r.returncode, 1, repr(name))
            self.assertIn("unsafe", r.stderr)

    def test_empty_symlink_target_refused(self):
        self.set_manifest(["tools/**", "L"])
        ref = self.crafted_commit({"tools": {"a": "1"}, "L": ("link", "")})
        r = self.run_script("--ref", ref, "--list")
        self.assertEqual(r.returncode, 1)
        self.assertIn("undeclared symlink", r.stderr)


class MainRobustnessTest(ExportCase):
    def test_value_error_is_a_clean_error(self):
        err = io.StringIO()
        with mock.patch.object(export_toolkit, "run", side_effect=ValueError("bad")):
            with contextlib.redirect_stderr(err):
                code = export_toolkit.main(["--check"])
        self.assertEqual(code, 1)
        self.assertIn("error: bad", err.getvalue())

    def run_main(self, *args):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = export_toolkit.main(
                ["--repo", str(self.repo), "--manifest", str(self.manifest), *args]
            )
        return code, err.getvalue()

    def test_failed_output_removes_created_parents(self):
        self.standard()
        out = self.tmp / "new" / "a" / "b"
        with mock.patch.object(export_toolkit.os, "symlink", side_effect=OSError("boom")):
            code, _ = self.run_main("--output", str(out))
        self.assertEqual(code, 1)
        self.assertFalse((self.tmp / "new").exists())

    def test_failed_tarball_removes_created_parents(self):
        self.standard()
        tb = self.tmp / "newt" / "deeper" / "x.tar.gz"
        with mock.patch.object(
            export_toolkit.tarfile, "open", side_effect=OSError("disk full")
        ):
            code, _ = self.run_main("--tarball", str(tb))
        self.assertEqual(code, 1)
        self.assertFalse((self.tmp / "newt").exists())

    def test_successful_export_keeps_new_parents(self):
        self.standard()
        out = self.tmp / "new" / "a"
        code, err = self.run_main("--output", str(out))
        self.assertEqual(code, 0, err)
        self.assertTrue((out / "README.md").exists())

    def test_umask_applies_to_directory_output_not_tarball(self):
        self.standard()
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        out = self.tmp / "out"
        tb = self.tmp / "t.tar.gz"
        self.assertEqual(self.run_main("--output", str(out))[0], 0)
        self.assertEqual(self.run_main("--tarball", str(tb))[0], 0)
        self.assertEqual(os.stat(out / "README.md").st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(out / "tools/run.sh").st_mode & 0o777, 0o700)
        with tarfile.open(tb) as t:
            self.assertEqual(t.getmember("t/README.md").mode & 0o777, 0o644)
            self.assertEqual(t.getmember("t/tools/run.sh").mode & 0o777, 0o755)

    def test_current_directory_and_dotdot_refused(self):
        self.standard()
        empty = self.tmp / "empty"
        empty.mkdir()
        for arg in (".", "./", str(empty)):
            r = self.run_script("--output", arg, cwd=empty)
            self.assertEqual(r.returncode, 1, arg)
            self.assertIn("current directory", r.stderr)
        r = self.run_script("--output", "..", cwd=empty)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(sorted(p.name for p in empty.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
