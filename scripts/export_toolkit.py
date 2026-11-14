#!/usr/bin/env python3
"""Export the project-neutral spec-driven toolkit from a committed git ref.

Files are selected by scripts/toolkit-export.toml (see its header for the glob
rules). The file list, the contents and, unless --manifest is given, the
manifest itself are read from git objects at the ref, never from the working
tree.
"""

import argparse
import functools
import gzip
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import unicodedata
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = "scripts/toolkit-export.toml"
TARBALL_SUFFIX = ".tar.gz"


class ExportError(Exception):
    pass


MAX_PATTERNS = 1000
MAX_PATTERN_LENGTH = 512
STAR = "*"
ANY = "?"
DOUBLE = "**"


def parse_set(pattern, i):
    """Parse a bracket class starting at pattern[i] == '['; return (token, next index)."""
    j = i + 1
    negate = j < len(pattern) and pattern[j] == "!"
    if negate:
        j += 1
    members = []
    first = True
    while True:
        if j >= len(pattern):
            raise ExportError("unterminated '[' in pattern")
        c = pattern[j]
        if c == "]" and not first:
            j += 1
            break
        first = False
        if j + 2 < len(pattern) and pattern[j + 1] == "-" and pattern[j + 2] != "]":
            lo, hi = c, pattern[j + 2]
            if lo > hi:
                raise ExportError(f"invalid range {lo}-{hi} in pattern")
            members.append((lo, hi))
            j += 3
        else:
            members.append((c, c))
            j += 1
    return ("set", negate, tuple(members)), j


@functools.lru_cache(maxsize=None)
def compile_pattern(pattern):
    """Compile a glob into a tuple of segments; a segment is DOUBLE or a token tuple."""
    if len(pattern) > MAX_PATTERN_LENGTH:
        raise ExportError(f"pattern longer than {MAX_PATTERN_LENGTH} characters")
    segments = []
    tokens = []
    start = 0
    i = 0

    def close(end):
        raw = pattern[start:end]
        if not raw:
            raise ExportError("empty path segment in pattern (leading, trailing or doubled '/')")
        if raw == "**":
            if not segments or segments[-1] != DOUBLE:
                segments.append(DOUBLE)  # consecutive '**' segments collapse
        else:
            segments.append(tuple(tokens))

    while i < len(pattern):
        c = pattern[i]
        if c == "/":
            close(i)
            tokens = []
            i += 1
            start = i
        elif c == "*":
            if not tokens or tokens[-1] != STAR:
                tokens.append(STAR)
            i += 1
        elif c == "?":
            tokens.append(ANY)
            i += 1
        elif c == "[":
            token, i = parse_set(pattern, i)
            tokens.append(token)
        else:
            tokens.append(("lit", c))
            i += 1
    close(len(pattern))
    return tuple(segments)


def token_matches(token, char):
    if token == ANY:
        return char != "/"
    kind = token[0]
    if kind == "lit":
        return token[1] == char
    if char == "/":
        return False
    found = any(lo <= char <= hi for lo, hi in token[2])
    return found != token[1]


def segment_matches(tokens, text):
    """Glob match of one path segment; linear backtracking over the last '*' only."""
    t = i = 0
    star = -1
    mark = 0
    while i < len(text):
        if t < len(tokens) and tokens[t] == STAR:
            star, mark = t, i
            t += 1
        elif t < len(tokens) and token_matches(tokens[t], text[i]):
            t += 1
            i += 1
        elif star != -1:
            t = star + 1
            mark += 1
            i = mark
        else:
            return False
    while t < len(tokens) and tokens[t] == STAR:
        t += 1
    return t == len(tokens)


def matches(pattern, path):
    """Segment-wise match; iterative DP over (pattern segment, path segment), polynomial."""
    segments = compile_pattern(pattern)
    parts = path.split("/")
    n = len(parts)
    following = [j == n for j in range(n + 1)]
    for i in reversed(range(len(segments))):
        segment = segments[i]
        current = [False] * (n + 1)
        if segment == DOUBLE:
            if i == len(segments) - 1:
                for j in range(n):
                    current[j] = True  # a trailing '**' needs at least one segment
            else:
                for j in range(n, -1, -1):
                    current[j] = following[j] or (j < n and current[j + 1])
        else:
            for j in range(n):
                current[j] = following[j + 1] and segment_matches(segment, parts[j])
        following = current
    return following[0]


def any_match(patterns, path):
    return any(matches(p, path) for p in patterns)


def classify(paths, include, exclude):
    included, excluded, unclassified = [], [], []
    for path in sorted(paths):
        if any_match(exclude, path):
            excluded.append(path)
        elif any_match(include, path):
            included.append(path)
        else:
            unclassified.append(path)
    return included, excluded, unclassified


def git_env():
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def git(repo, *args, check=True):
    """Run git in repo and return the CompletedProcess with bytes output."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
        env=git_env(),
    )
    if check and result.returncode != 0:
        message = result.stderr.decode(errors="replace").strip()
        raise ExportError(f"git {' '.join(args)} failed: {message}")
    return result


def git_text(repo, *args):
    return git(repo, *args).stdout.decode(errors="replace")


def has_control(text):
    return any(unicodedata.category(ch) == "Cc" for ch in text)


def parse_manifest(text, label):
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise ExportError(f"invalid manifest {label}: {error}") from error
    lists = {}
    for key in ("include", "exclude"):
        value = data.get(key)
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ExportError(f"manifest {label}: '{key}' must be a list of strings")
        if len(value) > MAX_PATTERNS:
            raise ExportError(
                f"manifest {label}: '{key}' has more than {MAX_PATTERNS} patterns"
            )
        for pattern in value:
            try:
                compile_pattern(pattern)
            except ExportError as error:
                raise ExportError(
                    f"manifest {label}: invalid pattern in '{key}': {pattern[:80]!r}: {error}"
                ) from error
        lists[key] = value
    symlinks = data.get("symlinks", {})
    if not isinstance(symlinks, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in symlinks.items()
    ):
        raise ExportError(
            f"manifest {label}: 'symlinks' must be a table of path = target strings"
        )
    for path, target in symlinks.items():
        if not target or has_control(target) or target.startswith("/"):
            raise ExportError(
                f"manifest {label}: unsafe or empty target for symlink {path!r}"
            )
        if unsafe_path(path):
            raise ExportError(f"manifest {label}: unsafe symlink path {path!r}")
    return lists["include"], lists["exclude"], dict(symlinks)


def load_manifest(repo, tree, override):
    if override:
        try:
            text = Path(override).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise ExportError(f"cannot read manifest {override}: {error}") from error
        return parse_manifest(text, str(override))
    result = git(repo, "cat-file", "blob", f"{tree}:{DEFAULT_MANIFEST}", check=False)
    if result.returncode != 0:
        raise ExportError(f"manifest {DEFAULT_MANIFEST} not found at the exported ref")
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExportError(f"manifest {DEFAULT_MANIFEST} is not UTF-8: {error}") from error
    return parse_manifest(text, f"{DEFAULT_MANIFEST} at the exported ref")


def resolve_tree(repo, ref):
    result = git(
        repo, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{tree}}", check=False
    )
    if result.returncode != 0:
        raise ExportError(f"cannot resolve ref {ref!r} to a tree")
    return result.stdout.decode().strip()


IGNORABLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0xE0100, 0xE01EF),
)


def drop_ignorable(text):
    """Remove Unicode default-ignorable code points, as git's core.protectHFS does."""
    return "".join(
        ch
        for ch in text
        if unicodedata.category(ch) != "Cf"
        and not any(lo <= ord(ch) <= hi for lo, hi in IGNORABLE_RANGES)
    )


def unsafe_path(path):
    if "\\" in path or has_control(path):
        return True
    return any(
        part in ("", ".", "..")
        or part.casefold() == ".git"
        or drop_ignorable(part).casefold() == ".git"
        for part in path.split("/")
    )


def read_tree(repo, tree):
    """Return {path: (mode, sha)} for every file in the tree."""
    out = git(repo, "ls-tree", "-r", "-z", "--full-tree", tree).stdout
    entries = {}
    submodules, unsafe, non_utf8 = [], [], []
    for record in out.split(b"\0"):
        if not record:
            continue
        meta, raw_path = record.split(b"\t", 1)
        mode, _kind, sha = meta.decode().split()
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError:
            non_utf8.append(raw_path.decode("utf-8", errors="replace"))
            continue
        if unsafe_path(path):
            unsafe.append(path)
        if mode == "160000":
            submodules.append(path)
        entries[path] = (mode, sha)
    if non_utf8:
        raise ExportError(
            "unsupported tracked paths (not valid UTF-8): " + ", ".join(sorted(non_utf8))
        )
    if unsafe:
        raise ExportError(
            "unsafe path components (empty, '.', '..', '.git', backslash or "
            "control characters): " + ", ".join(repr(p) for p in sorted(unsafe))
        )
    if submodules:
        raise ExportError(
            "submodules cannot be exported: " + ", ".join(sorted(submodules))
        )
    return entries


def blob(repo, sha):
    return git(repo, "cat-file", "blob", sha).stdout


def check_collisions(included):
    """Refuse paths that would collide on a case- or normalization-insensitive disk."""
    seen = {}
    for path in included:
        parts = path.split("/")
        for i in range(len(parts)):
            prefix = "/".join(parts[: i + 1])
            kind = "file" if i == len(parts) - 1 else "dir"
            key = unicodedata.normalize("NFC", drop_ignorable(prefix)).casefold()
            seen.setdefault(key, {}).setdefault(prefix, set()).add(kind)
    problems = []
    for originals in seen.values():
        kinds = set().union(*originals.values())
        if len(originals) > 1 or len(kinds) > 1:
            problems.append(" vs ".join(repr(o) for o in sorted(originals)))
    if problems:
        raise ExportError(
            "paths collide when case-folded (unsafe on case-insensitive file "
            "systems):\n" + "\n".join(f"  {p}" for p in sorted(problems))
        )


def resolve_lexically(path, target, declared):
    """Resolve a symlink target against its directory by exact string steps.

    Refuses a target that climbs above the root or passes through (or ends at)
    another declared symlink; returns the resolved path ('' for the root).
    """
    stack = []
    for part in [*path.split("/")[:-1], *target.split("/")]:
        if part in ("", "."):
            continue
        if part == "..":
            if not stack:
                raise ExportError(f"symlink {path} escapes the exported tree: {target!r}")
            stack.pop()
            continue
        stack.append(part)
        if "/".join(stack) in declared:
            raise ExportError(f"symlink {path} passes through another symlink: {target!r}")
    return "/".join(stack)


def validate_symlinks(repo, entries, included, declared):
    """Allow only the symlinks the manifest declares, with exactly the declared target."""
    exported = set(included)
    for path in included:
        parts = path.split("/")
        for i in range(1, len(parts)):
            if "/".join(parts[:i]) in declared:
                raise ExportError(f"{path} lies below the symlink {'/'.join(parts[:i])}")
    for path, target in sorted(declared.items()):
        entry = entries.get(path)
        if entry is None:
            raise ExportError(f"declared symlink {path} is missing at the exported ref")
        mode, sha = entry
        if mode != "120000":
            raise ExportError(f"declared symlink {path} is not a symlink in the tree")
        if path not in exported:
            raise ExportError(
                f"declared symlink {path} is not exported (check include and exclude)"
            )
        actual = blob(repo, sha)
        if actual != target.encode():
            raise ExportError(
                f"symlink {path} target mismatch: tree has "
                f"{actual.decode(errors='replace')!r}, manifest declares {target!r}"
            )
        resolved = resolve_lexically(path, target, declared)
        if resolved in exported or any(p.startswith(resolved + "/") for p in exported):
            continue
        raise ExportError(
            f"symlink {path} points at {resolved or '.'}, which is not part of the export"
        )
    for path in included:
        mode, sha = entries[path]
        if mode == "120000" and path not in declared:
            target = blob(repo, sha).decode(errors="replace")
            raise ExportError(
                f"undeclared symlink {path} -> {target!r}; declare it under "
                "[symlinks] in the manifest or remove it"
            )


def plan(repo, tree, include, exclude, declared):
    entries = read_tree(repo, tree)
    included, _excluded, unclassified = classify(entries, include, exclude)
    if unclassified:
        raise ExportError(
            "unclassified tracked paths (add them to include or exclude in "
            "the manifest):\n" + "\n".join(f"  {p}" for p in unclassified)
        )
    check_collisions(included)
    validate_symlinks(repo, entries, included, declared)
    return entries, included


def toplevel(repo):
    return Path(git_text(repo, "rev-parse", "--show-toplevel").strip()).resolve()


def refuse_inside_repo(destination, top):
    probe = Path(destination)
    if not probe.is_absolute():
        probe = Path.cwd() / probe
    while not os.path.exists(probe) and probe.parent != probe:
        if os.path.islink(probe):
            raise ExportError(
                f"{destination} passes through the dangling symlink {probe}"
            )
        probe = probe.parent  # '..' components are kept, so the OS resolves them
    real = Path(os.path.realpath(probe))
    for candidate in (real, *real.parents):
        if os.path.samefile(candidate, top):
            raise ExportError(f"{destination} is inside the repository work tree {top}")


def uncommitted_exported(repo, include, exclude):
    # core.fsmonitor names a command git would run during status; the repository's own
    # config must not be able to execute anything.
    out = git(
        repo,
        "-c",
        "core.fsmonitor=false",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
    ).stdout
    tokens = out.split(b"\0")
    changed = set()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if not token:
            continue
        status = token[:2].decode()
        paths = [os.fsdecode(token[3:])]
        if status[0] in "RC" and i < len(tokens):
            paths.append(os.fsdecode(tokens[i]))
            i += 1
        for path in paths:
            if any_match(include, path) and not any_match(exclude, path):
                changed.add(path)
    return sorted(changed)


def safe_parent(root, relative):
    # Deliberate defence in depth: planning, this walk, O_NOFOLLOW and the
    # post-write realpath check each stop symlink tricks on their own.
    """Create the parent directories below root without following symlinks."""
    current = root
    for part in relative.split("/")[:-1]:
        current = current / part
        if current.is_symlink():
            raise ExportError(f"refusing to write through the symlink {current}")
        if not current.exists():
            current.mkdir()
        elif not current.is_dir():
            raise ExportError(f"{current} exists and is not a directory")
    if not current.resolve().is_relative_to(root.resolve()):
        raise ExportError(f"{relative} would be written outside {root}")
    return current


def write_tree(repo, entries, included, destination):
    destination.mkdir(parents=True, exist_ok=True)
    umask = current_umask()
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    links = []
    for path in included:
        mode, sha = entries[path]
        parent = safe_parent(destination, path)
        target = parent / path.rsplit("/", 1)[-1]
        data = blob(repo, sha)
        if mode == "120000":
            os.symlink(data.decode("utf-8"), target)
            links.append(target)
            continue
        perm = (0o755 if mode == "100755" else 0o644) & ~umask
        fd = os.open(target, flags, perm)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            os.fchmod(handle.fileno(), perm)
    root = Path(os.path.realpath(destination))
    for link in links:
        if not Path(os.path.realpath(link)).is_relative_to(root):
            raise ExportError(f"symlink {link} resolves outside {destination}")


def write_tarball(repo, entries, included, handle, top_name):
    with gzip.GzipFile(fileobj=handle, mode="wb", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            dirs = {""}
            for path in included:
                dirs.update(str(p) for p in list(PurePosixPath(path).parents)[:-1])
            for d in sorted(dirs):
                info = tarfile.TarInfo(f"{top_name}/{d}".rstrip("/"))
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                info.mtime = 0
                tar.addfile(info)
            for path in included:
                mode, sha = entries[path]
                data = blob(repo, sha)
                info = tarfile.TarInfo(f"{top_name}/{path}")
                info.mtime = 0
                if mode == "120000":
                    info.type = tarfile.SYMTYPE
                    info.linkname = data.decode("utf-8")
                    info.mode = 0o777
                    tar.addfile(info)
                else:
                    info.size = len(data)
                    info.mode = 0o755 if mode == "100755" else 0o644
                    tar.addfile(info, io.BytesIO(data))


def current_umask():
    mask = os.umask(0)
    os.umask(mask)
    return mask


def make_parents(path):
    """Create missing directories; return the ones created (outermost first)."""
    missing = []
    probe = path
    while not probe.exists() and probe.parent != probe:
        missing.append(probe)
        probe = probe.parent
    created = []
    try:
        for directory in reversed(missing):
            directory.mkdir()
            created.append(directory)
    except BaseException:
        remove_created(created)
        raise
    return created


def remove_created(created):
    for directory in reversed(created):
        try:
            directory.rmdir()
        except OSError:
            pass


def discard(name):
    if name and os.path.lexists(name):
        os.unlink(name)


def export_directory(repo, entries, included, out):
    created = make_parents(out.parent)
    temp = None
    try:
        temp = Path(tempfile.mkdtemp(prefix=".export-", dir=out.parent))
        write_tree(repo, entries, included, temp)
        os.chmod(temp, 0o777 & ~current_umask())
        os.replace(temp, out)  # replaces an empty directory atomically
    except BaseException:
        if temp:
            shutil.rmtree(temp, ignore_errors=True)
        remove_created(created)
        raise


def export_tarball(repo, entries, included, tb):
    created = make_parents(tb.parent)
    name = None
    try:
        fd, name = tempfile.mkstemp(prefix=".export-", suffix=".tmp", dir=tb.parent)
        with os.fdopen(fd, "wb") as handle:
            write_tarball(repo, entries, included, handle, tb.name[: -len(TARBALL_SUFFIX)])
        os.chmod(name, 0o666 & ~current_umask())
        try:
            os.link(name, tb)  # fails if the file appeared meanwhile
        except FileExistsError as error:
            raise ExportError(f"{tb} already exists") from error
    except BaseException:
        discard(name)
        remove_created(created)
        raise
    discard(name)


def check_destinations(args, top):
    if args.output:
        out = Path(args.output)
        if out.name in ("", ".", "..") or (
            out.exists() and os.path.samefile(out, Path.cwd())
        ):
            raise ExportError(
                f"refusing to export into the current directory or a path "
                f"ending in '.' or '..': {args.output!r}"
            )
        refuse_inside_repo(out, top)
        if out.is_symlink():
            raise ExportError(f"{out} is a symbolic link")
        if out.exists() and (not out.is_dir() or any(out.iterdir())):
            raise ExportError(f"{out} exists and is not an empty directory")
    if args.tarball:
        tb = Path(args.tarball)
        stem = tb.name[: -len(TARBALL_SUFFIX)]
        if (
            not tb.name.endswith(TARBALL_SUFFIX)
            or stem in ("", ".", "..")
            or "\\" in stem
        ):
            raise ExportError(
                f"{tb} must be a .tar.gz file whose name is a single safe path component"
            )
        refuse_inside_repo(tb, top)
        if tb.exists() or tb.is_symlink():
            raise ExportError(f"{tb} already exists")


def run(args):
    repo = Path(args.repo).resolve()
    top = toplevel(repo)
    check_destinations(args, top)
    tree = resolve_tree(repo, args.ref)
    include, exclude, declared = load_manifest(repo, tree, args.manifest)
    entries, included = plan(repo, tree, include, exclude, declared)

    if args.check:
        print(
            f"OK: all {len(entries)} tracked paths classified at {args.ref}; "
            f"{len(included)} exported"
        )
        return
    dirty = uncommitted_exported(repo, include, exclude)
    if dirty:
        print(
            f"warning: uncommitted changes to exported paths are not exported "
            f"(only {args.ref} is): " + ", ".join(dirty),
            file=sys.stderr,
        )
    if args.list:
        print("\n".join(included))
    elif args.output:
        export_directory(repo, entries, included, Path(args.output))
        print(f"Exported {len(included)} files from {args.ref} to {args.output}")
    else:
        export_tarball(repo, entries, included, Path(args.tarball))
        print(f"Exported {len(included)} files from {args.ref} to {args.tarball}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="validate the manifest")
    mode.add_argument("--list", action="store_true", help="print exported paths")
    mode.add_argument("--output", metavar="DIR", help="write the tree into DIR")
    mode.add_argument("--tarball", metavar="FILE", help="write a .tar.gz file")
    parser.add_argument("--ref", default="HEAD", help="git ref to export (HEAD)")
    parser.add_argument(
        "--manifest",
        help=f"manifest file overriding {DEFAULT_MANIFEST} read from the ref",
    )
    parser.add_argument("--repo", default=str(REPO_ROOT), help="repository root")
    args = parser.parse_args(argv)
    try:
        run(args)
    except (ExportError, OSError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
