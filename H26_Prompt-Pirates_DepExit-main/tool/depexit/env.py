"""Isolated virtual environments, installs, import smoke tests and pytest runs.

Everything here is executed for real; results are stored as observed evidence.
"""
import json
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from .manifest import requirement_files
from .util import canon, run, tail, venv_python

SMOKE_SCRIPT = r'''
import importlib, importlib.metadata as md, json, re, sys, warnings
req = json.loads(sys.argv[1])
canon = lambda n: re.sub(r"[-_.]+", "-", n).lower()
out = {"python": sys.version.split()[0], "imports": {}, "dists": {}, "pkg_map": {}}
try:
    pkg_map = md.packages_distributions()
except Exception:
    pkg_map = {}
out["pkg_map"] = {k: [canon(x) for x in v] for k, v in pkg_map.items()}
for d in md.distributions():
    name = d.metadata["Name"]
    if not name:
        continue
    out["dists"][canon(name)] = {
        "version": d.version,
        "console_scripts": sorted({e.name for e in d.entry_points if e.group == "console_scripts"}),
        "requires": d.requires or [],
    }
modules = list(req.get("modules", []))
for dist in req.get("dists", []):
    for mod, owners in out["pkg_map"].items():
        if dist in owners and not mod.startswith("_") and mod not in modules:
            modules.append(mod)
for m in modules:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            importlib.import_module(m)
            ok, err = True, None
        except BaseException as e:
            ok, err = False, "%s: %s" % (type(e).__name__, e)
    out["imports"][m] = {"ok": ok, "error": err,
                         "warnings": sorted({"%s: %s" % (w.category.__name__, w.message) for w in caught})[:5]}
print("@@SMOKE@@" + json.dumps(out))
'''


def resolve_python(spec):
    if os.path.isfile(spec):
        return spec
    for cand in (spec, "python" + spec, "python3." + spec.split(".")[-1] if spec.startswith("3.") else None):
        if cand and shutil.which(cand):
            return shutil.which(cand)
    if os.name == "nt" and shutil.which("py"):
        return "py:-" + spec
    return None


def uv_bin():
    """uv makes venvs ~10x faster and can download missing Python versions.
    It is a dependency of DepExit, so it is normally available even if not on PATH."""
    if os.environ.get("DEPEXIT_NO_UV") == "1":
        return None
    found = shutil.which("uv")
    if found:
        return found
    try:
        from uv import find_uv_bin
        return find_uv_bin()
    except Exception:
        return None


def use_uv():
    return uv_bin() is not None


def install_routes(repo):
    """How the project can be installed. Each route is verified separately."""
    repo = Path(repo)
    routes = []
    if requirement_files(repo):
        routes.append("requirements")
    pyproject = repo / "pyproject.toml"
    if pyproject.exists() and "[project]" in pyproject.read_text(encoding="utf-8"):
        routes.append("pyproject")
    if not routes and (repo / "setup.py").exists():
        routes.append("pyproject")
    return routes


def create(env_dir, python_spec, repo, route):
    """Create a venv and install the project the given way. Returns an evidence dict."""
    env_dir = Path(env_dir)
    if env_dir.exists():
        shutil.rmtree(env_dir)
    steps = []
    if use_uv():
        steps.append(run([uv_bin(), "venv", "--python", python_spec, str(env_dir)], timeout=600))
    else:
        py = resolve_python(python_spec)
        if py is None:
            return {"ok": False, "route": route, "steps": [], "error": "Python %s not found" % python_spec}
        cmd = ["py", py.split(":", 1)[1]] if py.startswith("py:") else [py]
        steps.append(run(cmd + ["-m", "venv", str(env_dir)], timeout=600))
    if steps[-1]["returncode"] != 0:
        return _env_result(False, route, steps, "venv creation failed")

    py = venv_python(env_dir)
    if use_uv():
        pip = [uv_bin(), "pip", "install", "--python", str(py)]
    else:
        pip = [str(py), "-m", "pip", "install", "--disable-pip-version-check", "-q"]
    if route == "requirements":
        args = []
        for f in requirement_files(repo):
            args += ["-r", str(f)]
        steps.append(run(pip + args, cwd=repo, timeout=900))
    else:
        steps.append(run(pip + [str(repo)], cwd=repo, timeout=900))
    install_ok = steps[-1]["returncode"] == 0
    # pytest is needed to verify even when the project forgot to declare it
    steps.append(run(pip + ["pytest"], cwd=repo, timeout=600))
    return _env_result(install_ok, route, steps, None if install_ok else "dependency install failed")


def _env_result(ok, route, steps, error):
    return {
        "ok": ok,
        "route": route,
        "error": error,
        "steps": [{"cmd": s["cmd"], "returncode": s["returncode"], "seconds": s["seconds"],
                   "output_tail": tail(s["stdout"] + "\n" + s["stderr"], 25)} for s in steps],
    }


def smoke(env_dir, modules=(), dists=()):
    py = venv_python(env_dir)
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as fh:
        fh.write(SMOKE_SCRIPT)
        script = fh.name
    try:
        res = run([str(py), "-I", script, json.dumps({"modules": list(modules), "dists": list(dists)})],
                  cwd=tempfile.gettempdir(), timeout=300)
    finally:
        os.unlink(script)
    for line in res["stdout"].splitlines():
        if line.startswith("@@SMOKE@@"):
            return json.loads(line[len("@@SMOKE@@"):])
    return {"error": tail(res["stderr"], 15), "imports": {}, "dists": {}, "pkg_map": {}}


def run_tests(env_dir, repo, timeout=900):
    """Run pytest in the env and return per-test outcomes plus warnings."""
    env_dir, repo = Path(env_dir), Path(repo)
    junit = env_dir / "junit.xml"
    if junit.exists():
        junit.unlink()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("VIRTUAL_ENV", None)
    res = run([str(venv_python(env_dir)), "-m", "pytest", "-q", "-rfE", "-p", "no:cacheprovider",
               "-W", "default", "--junitxml=%s" % junit], cwd=repo, timeout=timeout, env=env)
    tests = {}
    if junit.exists():
        try:
            for case in ET.parse(junit).getroot().iter("testcase"):
                tid = "%s::%s" % (case.get("classname", ""), case.get("name", ""))
                outcome, message = "passed", ""
                for tag in ("failure", "error", "skipped"):
                    el = case.find(tag)
                    if el is not None:
                        outcome = {"failure": "failed"}.get(tag, tag)
                        message = normalise_path_text(el.get("message") or "")[:300]
                        detail = (el.text or "")
                        if detail:
                            message = (message + "\n" + normalise_path_text(tail(detail, 12))).strip()[:1500]
                        break
                tests[tid] = {"outcome": outcome, "message": message}
        except ET.ParseError:
            pass
    counts = {}
    for t in tests.values():
        counts[t["outcome"]] = counts.get(t["outcome"], 0) + 1
    if not tests and res["returncode"] not in (0, 5):
        counts["collection_error"] = 1
    return {
        "command": res["cmd"],
        "returncode": res["returncode"],
        "seconds": res["seconds"],
        "counts": counts,
        "tests": tests,
        "warnings": _warnings(res["stdout"]),
        "output_tail": tail(res["stdout"] + "\n" + res["stderr"], 30),
    }


def normalise_path_text(text):
    """Strip machine-specific paths so evidence is comparable between runs."""
    text = (text or "").replace("\\", "/")
    text = re.sub(r"[^\s'\"(]*?/(?:baseline-src|candidates/[^/]+)/", "", text)
    text = re.sub(r"[^\s'\"(]*?/site-packages/", "site-packages/", text)
    return text


def _warnings(stdout):
    found, inside = [], False
    for line in stdout.splitlines():
        if "warnings summary" in line:
            inside = True
            continue
        if inside and (line.startswith("-- Docs") or line.startswith("====")):
            inside = False
        if inside and "Warning" in line and not line.strip().startswith("warnings.warn"):
            text = normalise_path_text(line.strip())
            if text not in found:
                found.append(text[:300])
    return found


def dist_for_module(top, pkg_map):
    owners = pkg_map.get(top) or []
    return owners[0] if owners else canon(top)


def python_version(spec):
    if use_uv():
        res = run([uv_bin(), "python", "find", spec], timeout=120)
        if res["returncode"] == 0:
            ver = run([res["stdout"].strip(), "-c", "import sys;print(sys.version.split()[0])"])
            return ver["stdout"].strip()
    py = resolve_python(spec)
    if py and not py.startswith("py:"):
        return run([py, "-c", "import sys;print(sys.version.split()[0])"])["stdout"].strip()
    return spec

