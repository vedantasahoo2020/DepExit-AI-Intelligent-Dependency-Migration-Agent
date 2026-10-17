"""Read dependency declarations from requirements files and pyproject.toml."""
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib
from pathlib import Path

from packaging.requirements import InvalidRequirement, Requirement

from .util import canon


def _group_for(filename):
    low = filename.lower()
    if any(k in low for k in ("dev", "test", "lint", "doc")):
        return "dev"
    return "runtime"


def parse_req_line(line):
    """Return a Requirement for a requirements.txt line, or None."""
    text = line.split(" #", 1)[0].strip()
    if not text or text.startswith(("#", "-", "--")) or "://" in text:
        return None
    try:
        return Requirement(text)
    except InvalidRequirement:
        return None


def requirement_files(root):
    return sorted(Path(root).glob("requirements*.txt"))


def load(root):
    """All declarations, each tied to the file and line it came from."""
    root = Path(root)
    entries, commented = [], []
    for path in requirement_files(root):
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            req = parse_req_line(line)
            if req is None:
                body = line.strip().lstrip("#").strip()
                if line.strip().startswith("#") and parse_req_line(body):
                    commented.append({"file": path.name, "line": no, "text": line.strip()})
                continue
            entries.append(_entry(req, path.name, no, _group_for(path.name), line.strip()))

    pyproject = root / "pyproject.toml"
    requires_python = None
    if pyproject.exists():
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        lines = pyproject.read_text(encoding="utf-8").splitlines()
        project = data.get("project", {})
        requires_python = project.get("requires-python")
        for spec in project.get("dependencies", []):
            entries.append(_entry_from_spec(spec, "pyproject.toml", lines, "runtime"))
        for extra, specs in project.get("optional-dependencies", {}).items():
            for spec in specs:
                entries.append(_entry_from_spec(spec, "pyproject.toml", lines, "extra:" + extra))
        for spec in data.get("build-system", {}).get("requires", []):
            entries.append(_entry_from_spec(spec, "pyproject.toml", lines, "build"))
    entries = [e for e in entries if e]
    return {
        "entries": entries,
        "commented_out": commented,
        "requires_python": requires_python,
        "files": [p.name for p in requirement_files(root)] + (["pyproject.toml"] if pyproject.exists() else []),
    }


def _entry_from_spec(spec, filename, lines, group):
    try:
        req = Requirement(spec)
    except InvalidRequirement:
        return None
    quoted = ('"%s"' % spec, "'%s'" % spec)
    line_no = next((i for i, ln in enumerate(lines, 1) if any(q in ln for q in quoted)), None)
    return _entry(req, filename, line_no, group, spec)


def _entry(req, filename, line, group, raw):
    pins = [s.version for s in req.specifier if s.operator in ("==", "===")]
    return {
        "name": canon(req.name),
        "display_name": req.name,
        "specifier": str(req.specifier),
        "pinned": pins[0] if len(pins) == 1 and "*" not in pins[0] else None,
        "marker": str(req.marker) if req.marker else None,
        "file": filename,
        "line": line,
        "group": group,
        "raw": raw,
    }


def by_name(manifest, include_build=False):
    out = {}
    for e in manifest["entries"]:
        if e["group"] == "build" and not include_build:
            continue
        out.setdefault(e["name"], []).append(e)
    return out
