"""Apply a migration proposal to a throw-away copy of the repository."""
import difflib
import re
import shutil
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement

from .manifest import parse_req_line, requirement_files
from .util import canon, skipped


def copy_tree(src, dst):
    dst = Path(dst)
    if dst.exists():
        shutil.rmtree(dst)
    def ignore(directory, names):
        if ".depexit-ignore" in names:  # e.g. DepExit's own folder inside a forked target repo
            return list(names)
        return [n for n in names if skipped([n])]

    shutil.copytree(src, dst, ignore=ignore)
    return dst


def relocate(text, change):
    """Make an AI edit match the file even if the model got whitespace slightly wrong.

    Exact match first. Otherwise compare line-by-line ignoring leading/trailing spaces and blank lines;
    if exactly one block matches, use the file's real text (and shift the replacement's indentation).
    Returns True when the change now matches exactly once.
    """
    find = change["find"]
    if text.count(find) == 1:
        return True
    want = [l.strip() for l in find.splitlines() if l.strip()]
    if not want:
        return False
    lines = text.splitlines(keepends=True)
    nonblank = [(i, l.strip()) for i, l in enumerate(lines) if l.strip()]
    hits = []
    for k in range(len(nonblank) - len(want) + 1):
        if [t for _, t in nonblank[k:k + len(want)]] == want:
            hits.append((nonblank[k][0], nonblank[k + len(want) - 1][0]))
    if len(hits) != 1:
        return False
    start, end = hits[0]
    real = "".join(lines[start:end + 1])
    if not find.endswith("\n"):
        real = real.rstrip("\r\n")
    first_find = next(l for l in find.splitlines() if l.strip())
    real_indent = lines[start][:len(lines[start]) - len(lines[start].lstrip())]
    find_indent = first_find[:len(first_find) - len(first_find.lstrip())]
    replace = change["replace"]
    if real_indent != find_indent and real_indent.startswith(find_indent):
        extra = real_indent[len(find_indent):]
        replace = "".join(extra + l if l.strip() else l for l in replace.splitlines(keepends=True))
    change["find"], change["replace"], change["relocated"] = real, replace, True
    return text.count(real) == 1


def validate(root, proposal):
    """Check code changes can be applied before spending time on installs."""
    problems = []
    root = Path(root).resolve()
    for i, ch in enumerate(proposal["code_changes"]):
        path = (root / ch["file"]).resolve()
        if root not in path.parents and path != root:
            problems.append("change %d: path %s escapes the repository" % (i, ch["file"]))
            continue
        if not path.exists():
            if ch["find"] == "":
                continue  # creating a new file is allowed
            problems.append("change %d: file %s does not exist" % (i, ch["file"]))
            continue
        text = path.read_text(encoding="utf-8")
        if relocate(text, ch):
            continue
        n = text.count(ch["find"])
        if n != 1:
            problems.append("change %d: 'find' text occurs %d times in %s (must be exactly once)" % (i, n, ch["file"]))
    for a in proposal["add_requirements"]:
        try:
            Requirement(a["spec"])
        except InvalidRequirement as exc:
            problems.append("invalid requirement %r: %s" % (a["spec"], exc))
    return problems


def apply(root, proposal):
    root = Path(root)
    log, errors = [], []
    _edit_manifests(root, proposal, log)
    for ch in proposal["code_changes"]:
        path = root / ch["file"]
        if not path.exists() and ch["find"] == "":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(ch["replace"], encoding="utf-8", newline="\n")
            log.append("created %s" % ch["file"])
            continue
        if not path.exists():
            errors.append("missing file %s" % ch["file"])
            continue
        text = path.read_text(encoding="utf-8")
        if not relocate(text, ch):
            errors.append("could not apply change to %s ('find' matched %d times)" % (ch["file"], text.count(ch["find"])))
            continue
        path.write_text(text.replace(ch["find"], ch["replace"], 1), encoding="utf-8", newline="\n")
        log.append("edited %s" % ch["file"])
    return {"log": log, "errors": errors}


# ------------------------------------------------------------------ manifests
_PY_LINE = re.compile(r"""^(\s*)(["'])([^"']+)\2(\s*,?\s*(#.*)?)$""")


def _edit_manifests(root, proposal, log):
    remove = {canon(n) for n in proposal["remove_requirements"]}
    adds = {}
    for a in proposal["add_requirements"]:
        adds[canon(Requirement(a["spec"]).name)] = a
    placed = set()

    for f in requirement_files(root):
        lines, out = f.read_text(encoding="utf-8").splitlines(), []
        for line in lines:
            req = parse_req_line(line)
            name = canon(req.name) if req else None
            if name in adds:
                out.append(adds[name]["spec"])
                placed.add((name, f.name))
                log.append("%s: '%s' -> '%s'" % (f.name, line.strip(), adds[name]["spec"]))
            elif name in remove:
                log.append("%s: removed '%s'" % (f.name, line.strip()))
            else:
                out.append(line)
        f.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")

    pyproject = root / "pyproject.toml"
    py_lines = pyproject.read_text(encoding="utf-8").splitlines() if pyproject.exists() else []
    in_deps, section, dep_start = False, "", None
    new_lines = []
    for line in py_lines:
        stripped = line.strip()
        if stripped.startswith("[") and not in_deps:
            section = stripped
        if not in_deps and section in ("[project]", "[project.optional-dependencies]") and re.match(r"^[\w-]+\s*=\s*\[\s*$", stripped):
            in_deps = True
            new_lines.append(line)
            if stripped.startswith("dependencies") and section == "[project]":
                dep_start = len(new_lines)
            continue
        if in_deps:
            if stripped.startswith("]"):
                in_deps = False
            else:
                m = _PY_LINE.match(line)
                if m:
                    try:
                        name = canon(Requirement(m.group(3)).name)
                    except InvalidRequirement:
                        name = None
                    if name in adds:
                        new_lines.append('%s"%s",' % (m.group(1), adds[name]["spec"]))
                        placed.add((name, "pyproject.toml"))
                        log.append("pyproject.toml: '%s' -> '%s'" % (m.group(3), adds[name]["spec"]))
                        continue
                    if name in remove:
                        log.append("pyproject.toml: removed '%s'" % m.group(3))
                        continue
        new_lines.append(line)

    # brand-new requirements go where that group lives
    req_names = {p.name for p in requirement_files(root)}
    for name, a in adds.items():
        if any(n == name for n, _ in placed):
            continue
        if a["group"] == "dev":
            target = next((n for n in ("requirements-dev.txt", "requirements_dev.txt", "requirements-test.txt") if n in req_names), None)
            target = target or ("requirements.txt" if "requirements.txt" in req_names else None)
            targets = [target] if target else []
        else:
            targets = ["requirements.txt"] if "requirements.txt" in req_names else []
            if dep_start is not None:
                indent = "    "
                new_lines.insert(dep_start, '%s"%s",' % (indent, a["spec"]))
                log.append("pyproject.toml: added '%s'" % a["spec"])
        for t in targets:
            path = root / t
            path.write_text(path.read_text(encoding="utf-8").rstrip("\n") + "\n" + a["spec"] + "\n", encoding="utf-8", newline="\n")
            log.append("%s: added '%s'" % (t, a["spec"]))
    if pyproject.exists():
        pyproject.write_text("\n".join(new_lines) + "\n", encoding="utf-8", newline="\n")


def diff_trees(before, after):
    """Unified diff of text files that differ between two trees."""
    before, after = Path(before), Path(after)
    chunks = []
    files = sorted({p.relative_to(after).as_posix() for p in after.rglob("*") if p.is_file()}
                   | {p.relative_to(before).as_posix() for p in before.rglob("*") if p.is_file()})
    for rel in files:
        if skipped(rel.split("/")):
            continue
        a, b = before / rel, after / rel
        try:
            ta = a.read_text(encoding="utf-8").splitlines(keepends=True) if a.exists() else []
            tb = b.read_text(encoding="utf-8").splitlines(keepends=True) if b.exists() else []
        except UnicodeDecodeError:
            continue
        if ta != tb:
            chunks.extend(difflib.unified_diff(ta, tb, "a/" + rel, "b/" + rel))
    return "".join(c if c.endswith("\n") else c + "\n" for c in chunks)
