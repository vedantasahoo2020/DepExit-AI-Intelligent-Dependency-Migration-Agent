"""Turn verified results into a reviewable git branch in the user's own clone.

Nothing is pushed: the maintainer reviews the branch and pushes / opens a PR themselves.
"""
import json
import shutil
from datetime import datetime
from pathlib import Path

from .util import run

PATCHES = {"migration": "combined-migration.patch", "organizer": "organizer.patch"}


def create_branch(repo, capsule_dir, which=("migration", "organizer"), branch=None):
    repo, capsule_dir = Path(repo).resolve(), Path(capsule_dir).resolve()
    steps = []

    def git(*args):
        res = run(["git", "-C", str(repo)] + list(args), timeout=120)
        steps.append({"cmd": ("git " + " ".join(args)).splitlines()[0], "returncode": res["returncode"],
                      "output": (res["stdout"] + res["stderr"]).strip()[-600:]})
        return res

    if git("rev-parse", "--is-inside-work-tree")["returncode"] != 0:
        return {"ok": False, "error": "%s is not a git repository" % repo, "steps": steps}
    if git("status", "--porcelain")["stdout"].strip():
        return {"ok": False, "error": "working tree has uncommitted changes; commit or stash them first", "steps": steps}

    index = json.loads((capsule_dir / "capsule.json").read_text(encoding="utf-8"))
    patches = [(w, capsule_dir / PATCHES[w]) for w in which if (capsule_dir / PATCHES[w]).exists()]
    if not patches:
        return {"ok": False, "error": "no verified patch found in %s" % capsule_dir, "steps": steps}

    branch = branch or "depexit/exit-%s" % datetime.now().strftime("%Y%m%d-%H%M%S")
    original = git("rev-parse", "--abbrev-ref", "HEAD")["stdout"].strip() or None
    if git("checkout", "-b", branch)["returncode"] != 0:
        return {"ok": False, "error": "could not create branch %s" % branch, "steps": steps}

    applied = []
    for what, patch in patches:
        # Apply to the index (LF content as stored in git), so Windows CRLF checkouts don't break the patch.
        if git("apply", "--cached", "--check", str(patch))["returncode"] == 0:
            git("apply", "--cached", str(patch))
        elif git("apply", "--cached", "--3way", str(patch))["returncode"] != 0:
            git("reset", "-q")
            steps.append({"cmd": "skip " + patch.name, "returncode": 1,
                          "output": "%s does not apply to this checkout (is it the same repository/commit?)" % patch.name})
            continue
        git("commit", "-m", _message(what, index))
        git("checkout", "HEAD", "--", ".")  # bring the working tree in line with the commit
        applied.append(what)

    if not applied:
        git("checkout", "-q", original or "-")
        git("branch", "-D", branch)
        return {"ok": False, "error": "no patch applied; branch removed", "steps": steps}

    # ship the evidence with the change
    dest = repo / "dependency-exit-capsules"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(capsule_dir, dest, ignore=shutil.ignore_patterns("*.pyc"))
    git("add", "dependency-exit-capsules")
    git("commit", "-m", "docs: add DepExit Dependency Exit Capsules (evidence for this branch)")
    return {"ok": bool(applied), "branch": branch, "applied": applied, "steps": steps,
            "next": "review with `git log -p %s`, then `git push -u origin %s` and open a PR" % (branch, branch)}


def _message(what, index):
    if what == "organizer":
        org = index.get("organizer") or {}
        lines = ["chore(deps): organize dependency manifests (DepExit)", ""]
        lines += ["- %s %s: %s" % (a["action"], a["dependency"], a["reason"]) for a in org.get("actions", [])]
        lines += ["", "Verification: %s" % org.get("verdict")]
        return "\n".join(lines)
    deps = [d for d in index.get("all_dependencies", []) if d.get("verdict") in ("verified_improvement", "verified_no_regression")]
    lines = ["fix(deps): exit %d risky dependenc%s (DepExit)" % (len(deps), "y" if len(deps) == 1 else "ies"), ""]
    lines += ["- %s: %s (risk %s)" % (d["name"], d["verdict"], d["level"]) for d in deps]
    comb = index.get("combined_migration") or {}
    for route, r in (comb.get("comparison") or {}).get("routes", {}).items():
        lines.append("- tests (%s install): %s -> %s" % (route, r["tests_before"], r["tests_after"]))
    lines += ["", "AI proposals by %s; verdicts from executed installs and tests." % index["run"].get("ai_model")]
    return "\n".join(lines)
