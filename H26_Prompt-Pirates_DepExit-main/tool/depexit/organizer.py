"""Auto-Organizer: rebuild the dependency manifests from observed facts.

Rules (no AI):
  * remove runtime declarations the code never imports or references
  * declare packages the code imports but nobody declared (lower bound = version observed installed)
  * move packages that only tests use into the dev requirements
  * sort and de-duplicate, keep requirements.txt and pyproject.toml listing the same runtime set
  * keep disabled (commented-out) lines, but group them at the bottom

Gemma (optional) writes a short "why is this here" comment for every requirement.
Those comments are marked "(AI)" in the files.
The organized tree is then installed and tested like any other migration.
"""
import json
import re
from pathlib import Path

from packaging.requirements import Requirement

from .manifest import parse_req_line, requirement_files
from .util import canon

TEST_HINTS = ("tests/", "test/", "testing/")


def _is_test_file(rel):
    name = rel.rsplit("/", 1)[-1]
    return rel.startswith(TEST_HINTS) or "/tests/" in rel or name.startswith("test_") or name == "conftest.py"


def plan(deps):
    """Decide what the organized manifests should contain. Returns plan dict with reasons."""
    actions = []
    runtime, dev = {}, {}
    for d in deps:
        decl = d["declarations"]
        groups = {e["group"] for e in decl}
        files = {u["file"] for u in d["usage"]["imports"]}
        in_code = any(not _is_test_file(f) for f in files)
        in_tests = any(_is_test_file(f) for f in files)
        ids = {i["id"] for i in d["indicators"]}
        req_spec = next((e["raw"].split(" #")[0].strip() for e in decl if e["file"].startswith("requirements")
                         and e["group"] == "runtime"), None)
        dev_spec = next((e["raw"].split(" #")[0].strip() for e in decl if e["group"] == "dev"), None)
        py_spec = next((e["raw"] for e in decl if e["file"] == "pyproject.toml" and e["group"] == "runtime"), None)

        if "unused" in ids and "runtime" in groups:
            actions.append({"action": "remove", "dependency": d["name"],
                            "reason": "declared as runtime but never imported or referenced",
                            "evidence": next(i["detail"] for i in d["indicators"] if i["id"] == "unused")})
            continue
        if not decl:
            spec = d["name"]  # observed installed version is recorded in the evidence, not forced as a pin
            target = runtime if in_code else dev
            target[d["name"]] = {"req": spec, "py": spec}
            actions.append({"action": "declare", "dependency": d["name"], "spec": spec,
                            "group": "runtime" if in_code else "dev",
                            "reason": "imported by the code but not declared (was only installed transitively)",
                            "evidence": ", ".join("%s:%s" % (i["file"], i["line"]) for i in d["usage"]["imports"][:4])
                            + ("; installed transitively as %s" % d["installed_version"] if d.get("installed_version") else "")})
            continue
        if "runtime" in groups and in_tests and not in_code and not d["text_references"]:
            spec = req_spec or py_spec
            dev[d["name"]] = {"req": spec, "py": None}
            actions.append({"action": "move_to_dev", "dependency": d["name"],
                            "reason": "only test files import it", "evidence": ", ".join(sorted(files))})
            continue
        if "runtime" in groups:
            runtime[d["name"]] = {"req": req_spec or py_spec, "py": py_spec or req_spec}
            if req_spec and py_spec and Requirement(req_spec).specifier != Requirement(py_spec).specifier:
                actions.append({"action": "flag_conflict", "dependency": d["name"],
                                "reason": "requirements.txt and pyproject.toml disagree; left for the migration step",
                                "evidence": "%s vs %s" % (req_spec, py_spec)})
            elif req_spec and not py_spec:
                actions.append({"action": "sync", "dependency": d["name"], "reason": "missing from pyproject.toml",
                                "evidence": req_spec})
            elif py_spec and not req_spec and requirement_files_present(deps):
                actions.append({"action": "sync", "dependency": d["name"], "reason": "missing from requirements.txt",
                                "evidence": py_spec})
        if "dev" in groups:
            dev[d["name"]] = {"req": dev_spec, "py": None}
    return {"runtime": runtime, "dev": dev, "actions": actions}


def requirement_files_present(deps):
    return any(e["file"].startswith("requirements") for d in deps for e in d["declarations"])


def apply(root, the_plan, commented, annotations=None):
    """Rewrite the manifests in `root` (a throw-away copy) according to the plan."""
    root = Path(root)
    annotations = annotations or {}
    log = []

    def line(spec, name):
        note = annotations.get(name)
        return "%s  # (AI) %s" % (spec, note) if note else spec

    files = {p.name: p for p in requirement_files(root)}
    if "requirements.txt" in files or not files:
        body = ["# Runtime dependencies - organized by DepExit (sorted, synced with pyproject.toml)"]
        body += [line(v["req"], k) for k, v in sorted(the_plan["runtime"].items()) if v["req"]]
        disabled = [c["text"] for c in commented if c["file"] == "requirements.txt"]
        if disabled:
            body += ["", "# Disabled"] + disabled
        (root / "requirements.txt").write_text("\n".join(body) + "\n", encoding="utf-8", newline="\n")
        log.append("rewrote requirements.txt (%d runtime packages)" % len(the_plan["runtime"]))
    dev_name = next((n for n in files if n != "requirements.txt"), None)
    if dev_name or the_plan["dev"]:
        dev_name = dev_name or "requirements-dev.txt"
        body = ["# Development / test dependencies - organized by DepExit"]
        body += [line(v["req"], k) for k, v in sorted(the_plan["dev"].items()) if v["req"]]
        disabled = [c["text"] for c in commented if c["file"] == dev_name]
        if disabled:
            body += ["", "# Disabled"] + disabled
        (root / dev_name).write_text("\n".join(body) + "\n", encoding="utf-8", newline="\n")
        log.append("rewrote %s (%d dev packages)" % (dev_name, len(the_plan["dev"])))

    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8")
        specs = sorted((k, v["py"]) for k, v in the_plan["runtime"].items() if v["py"])
        block = "dependencies = [\n" + "".join('    "%s",\n' % s for _, s in specs) + "]"
        new, n = re.subn(r"(?ms)^dependencies\s*=\s*\[.*?^\]", block, text, count=1)
        if n:
            pyproject.write_text(new, encoding="utf-8", newline="\n")
            log.append("rewrote [project].dependencies in pyproject.toml (%d packages)" % len(specs))
    return log


def annotate(gemma, deps, the_plan):
    """Ask Gemma for a one-line purpose of each requirement (AI, clearly labelled)."""
    items = []
    for d in deps:
        if d["name"] in the_plan["runtime"] or d["name"] in the_plan["dev"]:
            items.append({"name": d["name"],
                          "used_at": ["%s:%s %s" % (u["file"], u["line"], u["code"]) for u in d["usage"]["usages"][:4]],
                          "non_python": [t["code"] for t in d["text_references"][:2]],
                          "pypi_summary": d["pypi"].get("summary")})
    prompt = ("For each dependency write a short purpose comment (max 8 words) describing what THIS project "
              "uses it for, based on the usage lines. Reply with ONE JSON object mapping name -> comment.\n\n"
              + json.dumps(items, indent=1))
    result = gemma._json("You annotate Python requirement files. Reply with JSON only.", prompt, "organizer:annotate")
    return {canon(k): str(v)[:70].replace("\n", " ") for k, v in result.items() if isinstance(v, str)}
