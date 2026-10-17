"""Find where third-party modules are imported and used (static AST analysis).

Handles aliases (``from requests import Session as S``), lazy imports inside
functions, private-API access, and monkeypatching of library attributes.
Also finds references in non-Python files (Makefile, Dockerfile, CI configs).
"""
import ast
import re
import sys
from pathlib import Path

from .util import skipped

STDLIB = set(sys.stdlib_module_names) | {"__future__"}
TEXT_FILES = ("Makefile", "Dockerfile", "Procfile", "tox.ini", "setup.cfg", "noxfile.py")
TEXT_SUFFIXES = (".yml", ".yaml", ".cfg", ".ini", ".sh", ".mk")


def iter_py(root):
    root = Path(root)
    for path in sorted(root.rglob("*.py")):
        if not skipped(path.relative_to(root).parts):
            yield path


def local_modules(root):
    """Top-level names that belong to the project itself."""
    root = Path(root)
    names = set()
    for path in iter_py(root):
        rel = path.relative_to(root).parts
        names.add(rel[0][:-3] if rel[0].endswith(".py") else rel[0])
    for src in ("src",):
        if (root / src).is_dir():
            names |= {p.name for p in (root / src).iterdir() if p.is_dir()}
    names |= {"conftest", "tests", "test"}
    return names


def _scope_of(node):
    names = []
    cur = getattr(node, "_parent", None)
    while cur is not None:
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(cur.name)
        cur = getattr(cur, "_parent", None)
    return ".".join(reversed(names)) or "<module>"


def _is_private(name):
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def scan(root):
    """Return {top_module: {"imports": [...], "usages": [...]}} for third-party modules."""
    root = Path(root)
    local = local_modules(root)
    found = {}
    errors = []
    for path in iter_py(root):
        rel = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8", errors="replace")
        lines = source.splitlines()
        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError as exc:
            errors.append({"file": rel, "error": str(exc)})
            continue
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                child._parent = node

        bindings = {}  # local name -> (top, qualified)
        import_lines = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top in STDLIB or top in local:
                        continue
                    bound = alias.asname or top
                    qual = alias.name if alias.asname else top
                    bindings[bound] = (top, qual)
                    import_lines.add(node.lineno)
                    _add_import(found, top, rel, node, lines, alias.name, None, alias.asname)
            elif isinstance(node, ast.ImportFrom):
                if node.level or not node.module:
                    continue
                top = node.module.split(".")[0]
                if top in STDLIB or top in local:
                    continue
                import_lines.add(node.lineno)
                for alias in node.names:
                    bound = alias.asname or alias.name
                    bindings[bound] = (top, "%s.%s" % (node.module, alias.name))
                    _add_import(found, top, rel, node, lines, node.module, alias.name, alias.asname)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Name) or node.id not in bindings:
                continue
            if node.lineno in import_lines and isinstance(node.ctx, ast.Store):
                continue
            top, qual = bindings[node.id]
            outer = node
            while isinstance(getattr(outer, "_parent", None), ast.Attribute) and outer._parent.value is outer:
                outer = outer._parent
            parent = getattr(outer, "_parent", None)
            grand = getattr(parent, "_parent", None)
            kind = "reference"
            if isinstance(outer, ast.Attribute) and isinstance(outer.ctx, ast.Store):
                kind = "monkeypatch"
            elif outer in getattr(parent, "decorator_list", []) or (
                    isinstance(parent, ast.Call) and parent.func is outer
                    and parent in getattr(grand, "decorator_list", [])):
                kind = "decorator"
            elif isinstance(parent, ast.Call) and parent.func is outer:
                kind = "call"
            elif isinstance(parent, ast.ClassDef) and outer in parent.bases:
                kind = "subclass"
            expr = ast.unparse(outer)
            attrs = expr.split(".")[1:]
            entry = {
                "file": rel,
                "line": node.lineno,
                "scope": _scope_of(node),
                "symbol": qual if not attrs else qual + "." + ".".join(attrs),
                "expr": expr,
                "kind": kind,
                "private_api": any(_is_private(a) for a in qual.split(".")[1:] + attrs),
                "code": lines[node.lineno - 1].strip() if node.lineno <= len(lines) else "",
            }
            found[top]["usages"].append(entry)
    for data in found.values():
        data["usages"].sort(key=lambda u: (u["file"], u["line"]))
    return found, errors


def _add_import(found, top, rel, node, lines, module, name, asname):
    scope = _scope_of(node)
    found.setdefault(top, {"imports": [], "usages": []})["imports"].append({
        "file": rel,
        "line": node.lineno,
        "statement": lines[node.lineno - 1].strip(),
        "module": module,
        "name": name,
        "alias": asname,
        "scope": scope,
        "lazy": scope != "<module>",
        "private_api": bool(name and _is_private(name)) or any(_is_private(p) for p in module.split(".")[1:]),
        "side_effect_only": False,
    })


def mark_side_effect_imports(found):
    """An import whose bound name is never used is kept only for side effects."""
    for top, data in found.items():
        used_files = {u["file"] for u in data["usages"]}
        for imp in data["imports"]:
            if imp["file"] not in used_files:
                imp["side_effect_only"] = True


def text_references(root, tokens):
    """Find tokens (dist names, console scripts) in Makefiles, Dockerfiles, CI configs."""
    root = Path(root)
    hits = []
    if not tokens:
        return hits
    pattern = re.compile(r"(?<![\w-])(%s)(?![\w-])" % "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True)), re.I)
    for path in sorted(root.rglob("*")):
        if not path.is_file() or skipped(path.relative_to(root).parts):
            continue
        name = path.name
        if not (name in TEXT_FILES or name.startswith("Dockerfile") or path.suffix in TEXT_SUFFIXES
                or ".github" in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for no, line in enumerate(text.splitlines(), 1):
            m = pattern.search(line)
            if m:
                hits.append({"file": path.relative_to(root).as_posix(), "line": no,
                             "token": m.group(1), "code": line.strip()})
    return hits
