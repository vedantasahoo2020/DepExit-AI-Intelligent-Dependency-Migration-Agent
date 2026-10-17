"""The DepExit pipeline: scan -> assess -> (Gemma) propose -> verify -> review -> capsule."""
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from packaging.requirements import Requirement
from rich.console import Console
from rich.table import Table

from . import __version__, capsule, env, manifest, migrate, organizer, pypi, risk, usage
from .util import canon, run

console = Console()
MAX_FILE_CHARS = 7000
MAX_CONTEXT_CHARS = 26000


# ------------------------------------------------------------------ target
def prepare_target(target, work):
    work = Path(work)
    if target.startswith(("http://", "https://", "git@")):
        dest = work / "target"
        if dest.exists():
            shutil.rmtree(dest)
        res = run(["git", "clone", "-c", "core.autocrlf=false", "--depth", "1", target, str(dest)], timeout=300)
        if res["returncode"] == -2:
            raise SystemExit("Git is not installed. Install it from https://git-scm.com/download/win and start DepExit again.")
        if res["returncode"] != 0:
            raise SystemExit("Could not download the repository (git clone failed). Check the URL and your internet connection.\n"
                             + res["stderr"][-500:])
        src = dest
    else:
        src = Path(target).resolve()
        if not src.is_dir():
            raise SystemExit("Target %s is not a directory" % target)
    commit = run(["git", "-C", str(src), "rev-parse", "HEAD"])["stdout"].strip() or None
    pristine = migrate.copy_tree(src, work / "baseline-src")
    return pristine, {"target": target, "commit": commit}


# ------------------------------------------------------------------ verification
def verify_tree(tree, label, work, python, routes, modules=(), dists=()):
    results = {}
    for route in routes:
        env_dir = Path(work) / "envs" / ("%s-%s" % (label, route))
        console.print("    [dim]installing ({}) ...[/dim]".format(route))
        setup = env.create(env_dir, python, tree, route)
        result = {"install": setup}
        if setup["steps"] and Path(env_dir).exists():
            result["smoke"] = env.smoke(env_dir, modules, dists)
            console.print("    [dim]running tests ({}) ...[/dim]".format(route))
            result["tests"] = env.run_tests(env_dir, tree)
        results[route] = result
    return results


def compare(base, cand, old_modules, residual):
    """Observed before/after comparison for every install route."""
    out = {}
    for route, b in base.items():
        c = cand.get(route) or {}
        bt = (b.get("tests") or {}).get("tests", {})
        ct = (c.get("tests") or {}).get("tests", {})
        fixed = sorted(t for t in bt if bt[t]["outcome"] in ("failed", "error") and ct.get(t, {}).get("outcome") == "passed")
        regressed = sorted(t for t in bt if bt[t]["outcome"] == "passed" and ct.get(t, {}).get("outcome") != "passed")
        new_tests = sorted(t for t in ct if t not in bt)
        bw = set((b.get("tests") or {}).get("warnings", []))
        cw = set((c.get("tests") or {}).get("warnings", []))
        out[route] = {
            "install_ok_before": b.get("install", {}).get("ok"),
            "install_ok_after": c.get("install", {}).get("ok"),
            "tests_before": (b.get("tests") or {}).get("counts", {}),
            "tests_after": (c.get("tests") or {}).get("counts", {}),
            "fixed": fixed,
            "regressed": regressed,
            "still_failing": sorted(t for t in ct if ct[t]["outcome"] in ("failed", "error") and t not in regressed),
            "new_tests": new_tests,
            "warnings_removed": sorted(bw - cw),
            "warnings_added": sorted(cw - bw),
            "regression_details": {t: ct.get(t, {}).get("message", "missing from run")[:800] for t in regressed},
        }
    return {"routes": out, "residual_references": residual}


def verdict(comparison, action, apply_result):
    routes = comparison["routes"].values()
    if apply_result["errors"]:
        return "not_applied"
    if any(r["install_ok_after"] is False and r["install_ok_before"] is not False for r in routes):
        return "install_failed"
    if any(r["regressed"] for r in routes):
        return "regression"
    if action in ("replace", "remove") and comparison["residual_references"]:
        return "incomplete"
    if any(r["fixed"] for r in routes) or any(r["install_ok_after"] and r["install_ok_before"] is False for r in routes):
        return "verified_improvement"
    return "verified_no_regression"


PASSING = {"verified_improvement", "verified_no_regression"}


def residual_refs(tree, modules, tokens, action):
    """After a replace/remove, is the old dependency still referenced anywhere?"""
    if action not in ("replace", "remove"):
        return []
    found, _ = usage.scan(tree)
    hits = []
    for m in modules:
        for i in found.get(m, {}).get("imports", []):
            hits.append("%s:%s %s" % (i["file"], i["line"], i["statement"]))
    for t in usage.text_references(tree, tokens):
        hits.append("%s:%s %s" % (t["file"], t["line"], t["code"]))
    from .manifest import by_name, load
    decl = by_name(load(tree)).get(tokens[0] if tokens else "", [])
    hits += ["%s:%s %s" % (d["file"], d["line"], d["raw"]) for d in decl]
    return hits


# ------------------------------------------------------------------ AI context
def build_context(dep, repo, python_version, all_declared):
    files = {}
    budget = MAX_CONTEXT_CHARS
    wanted = []
    for item in dep["usage"]["imports"] + dep["usage"]["usages"] + dep["text_references"]:
        if item["file"] not in wanted:
            wanted.append(item["file"])
    for e in dep["declarations"]:
        if e["file"] not in wanted:
            wanted.append(e["file"])
    for rel in wanted:
        path = Path(repo) / rel
        if not path.exists() or budget <= 0:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")[:min(MAX_FILE_CHARS, budget)]
        files[rel] = text
        budget -= len(text)
    return {
        "dependency": {"name": dep["name"], "installed_version": dep.get("installed_version"),
                       "modules": dep["modules"], "declarations": dep["declarations"]},
        "target_python": python_version,
        "risk_indicators": [{k: i[k] for k in ("id", "severity", "detail", "evidence")} for i in dep["indicators"]],
        "pypi": {k: v for k, v in dep["pypi"].items() if k in ("latest_version", "latest_release_date", "summary",
                                                               "requires_python_latest", "dev_status")},
        "usage_locations": [{k: u[k] for k in ("file", "line", "scope", "symbol", "kind", "code")} for u in dep["usage"]["usages"]][:40],
        "imports": [{k: i[k] for k in ("file", "line", "statement", "lazy", "side_effect_only")} for i in dep["usage"]["imports"]],
        "non_python_references": dep["text_references"],
        "other_declared_dependencies": sorted(all_declared - {dep["name"]}),
        "files": files,
    }


# ------------------------------------------------------------------ main
def set_console(c):
    """Let the web UI capture progress output."""
    global console
    console = c


def run_forecast(repo, work, versions, route, modules, dists, deps):
    """Run the ORIGINAL project on other Python versions to predict future breakage."""
    out = {"route": route, "versions": {}, "matrix": {}}
    for v in versions:
        console.print("  forecast: Python %s" % v)
        res = verify_tree(repo, "forecast-%s" % v, work, v, [route], modules, dists).get(route, {})
        smoke = res.get("smoke") or {}
        out["versions"][v] = {
            "python": smoke.get("python") or v,
            "install_ok": res.get("install", {}).get("ok"),
            "install_error_tail": "" if res.get("install", {}).get("ok") else
            "\n".join(s.get("output_tail", "")[-800:] for s in res.get("install", {}).get("steps", [])[1:2]),
            "tests": (res.get("tests") or {}).get("counts"),
            "warnings": len((res.get("tests") or {}).get("warnings", [])),
            "first_error": env.normalise_path_text(next((ln.strip() for ln in (res.get("tests") or {}).get("output_tail", "").splitlines()
                                 if re.search(r"\b\w+(Error|Exception)\b", ln)), "")),
        }
        for d in deps:
            cells = [smoke.get("imports", {}).get(m) for m in d["modules"]]
            cells = [c for c in cells if c]
            if not res.get("install", {}).get("ok"):
                state, detail = "install_failed", ""
            elif not cells:
                state, detail = "n/a", ""
            elif all(c["ok"] for c in cells):
                dep_warn = [w for c in cells for w in c.get("warnings", []) if "Deprecat" in w]
                state, detail = ("deprecated", dep_warn[0]) if dep_warn else ("ok", "")
            else:
                state, detail = "broken", next(c["error"] for c in cells if not c["ok"])
            out["matrix"].setdefault(d["name"], {})[v] = {"state": state, "detail": env.normalise_path_text(detail)[:200]}
    return out


def _current_deps(tree, pkg_map, installed):
    """Re-derive declarations/usage for a tree (the organizer works on the already-migrated code)."""
    man = manifest.load(tree)
    declared = manifest.by_name(man)
    found, _ = usage.scan(tree)
    usage.mark_side_effect_imports(found)
    mods = {}
    for top in found:
        mods.setdefault(env.dist_for_module(top, pkg_map), []).append(top)
    deps = []
    for name in sorted(set(declared) | set(mods)):
        modules = mods.get(name) or [m for m, owners in pkg_map.items() if name in owners and not m.startswith("_")]
        merged = {"imports": [], "usages": []}
        for m in modules:
            for k in merged:
                merged[k] += found.get(m, {}).get(k, [])
        scripts = (installed.get(name) or {}).get("console_scripts", [])
        refs = usage.text_references(tree, [name] + [x for x in scripts if x != name])
        decl = declared.get(name, [])
        if not decl and not merged["imports"]:
            continue
        deps.append({"name": name, "declarations": decl, "usage": merged, "text_references": refs,
                     "installed_version": (installed.get(name) or {}).get("version"),
                     "pypi": pypi.public(pypi.fetch(name)),
                     "indicators": risk.assess(name, decl, None, merged, refs, {}, modules)})
    return deps, man


def run_organizer(base_tree, base_results, work, python, routes, pkg_map, installed, gemma):
    console.rule("Auto-Organizer (manifests rebuilt from evidence, then tested)")
    deps, man = _current_deps(base_tree, pkg_map, installed)
    the_plan = organizer.plan(deps)
    notes, note_error = {}, None
    if gemma:
        try:
            notes = organizer.annotate(gemma, deps, the_plan)
        except Exception as exc:
            note_error = str(exc)
    tree = migrate.copy_tree(base_tree, Path(work) / "candidates" / "organized")
    log = organizer.apply(tree, the_plan, man["commented_out"], notes)
    for a in the_plan["actions"]:
        console.print("  %-13s %-16s %s" % (a["action"], a["dependency"], a["reason"]))
    cand = verify_tree(tree, "organized", work, python, routes)
    comparison = compare(base_results, cand, [], [])
    v = verdict(comparison, "upgrade", {"errors": []})
    console.print("  organizer verdict: [bold]%s[/bold]" % v)
    return {"actions": the_plan["actions"], "log": log, "ai_annotations": notes, "ai_error": note_error,
            "comparison": comparison, "verdict": v, "diff": migrate.diff_trees(base_tree, tree),
            "applies_on_top_of": "verified migration" if Path(base_tree) != Path(work) / "baseline-src" else "original repository"}


def analyze(target, out_dir="capsule", python="3.12", use_ai=True, only=None, max_deps=8,
            attempts=2, min_score=2, work=".depexit-work", model=None, forecast=None, organize=True):
    started = datetime.now(timezone.utc)
    work = Path(work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    gemma = None
    if use_ai:
        from .gemma import Gemma
        gemma = Gemma(model=model)
        console.print("[bold]AI:[/bold] %s via Gemini API" % gemma.model)
    else:
        console.print("[yellow]AI disabled (--no-ai): capsules will contain observed evidence only[/yellow]")

    console.rule("1. Target")
    repo, target_info = prepare_target(target, work)
    py_version = env.python_version(python)
    console.print("repo: %s  commit: %s  python: %s" % (target, (target_info["commit"] or "?")[:10], py_version))

    console.rule("2. Static scan")
    man = manifest.load(repo)
    declared = manifest.by_name(man)
    found, parse_errors = usage.scan(repo)
    usage.mark_side_effect_imports(found)
    console.print("%d declared dependencies in %s; %d third-party modules imported"
                  % (len(declared), ", ".join(man["files"]), len(found)))

    console.rule("3. Baseline (original dependencies, real install + tests)")
    routes = env.install_routes(repo)
    baseline = verify_tree(repo, "baseline", work, python, routes, modules=sorted(found), dists=sorted(declared))
    pkg_map, dists_installed = {}, {}
    for r in baseline.values():
        pkg_map.update((r.get("smoke") or {}).get("pkg_map", {}))
        dists_installed.update((r.get("smoke") or {}).get("dists", {}))
    for route, r in baseline.items():
        t = r.get("tests") or {}
        console.print("  %-12s install=%s tests=%s" % (route, "ok" if r["install"]["ok"] else "FAILED", t.get("counts")))

    # group modules under distributions
    mods_by_dist = {}
    for top in found:
        mods_by_dist.setdefault(env.dist_for_module(top, pkg_map), []).append(top)
    for name in declared:
        for mod, owners in pkg_map.items():
            if name in owners and not mod.startswith("_"):
                mods_by_dist.setdefault(name, [])
                if mod not in mods_by_dist[name]:
                    mods_by_dist[name].append(mod)
    candidates = sorted(set(declared) | set(mods_by_dist))

    console.rule("4. Risk assessment (PyPI + evidence)")
    deps = []
    for name in candidates:
        modules = sorted(set(mods_by_dist.get(name, [])) or {name.replace("-", "_")})
        merged = {"imports": [], "usages": []}
        for m in modules:
            for k in merged:
                merged[k] += found.get(m, {}).get(k, [])
        scripts = (dists_installed.get(name) or {}).get("console_scripts", [])
        tokens = [name] + [s for s in scripts if s != name]
        text_refs = usage.text_references(repo, tokens)
        meta = pypi.fetch(name)
        decl = declared.get(name, [])
        base_for_risk = {route: {"smoke": r.get("smoke") or {}, "tests": r.get("tests") or {}} for route, r in baseline.items()}
        indicators = risk.assess(name, decl, meta, merged, text_refs, base_for_risk, modules)
        if not decl and not merged["imports"]:
            continue
        deps.append({
            "name": name,
            "modules": modules,
            "console_scripts": scripts,
            "installed_version": (dists_installed.get(name) or {}).get("version"),
            "declarations": decl,
            "usage": merged,
            "text_references": text_refs,
            "pypi": pypi.public(meta),
            "indicators": indicators,
            "score": risk.score(indicators),
            "level": risk.level(indicators),
            "tokens": tokens,
        })
    deps.sort(key=lambda d: (-d["score"], d["name"]))
    _print_risk_table(deps)

    forecast_result = None
    if forecast:
        console.rule("Python forecast (original project on other Python versions)")
        forecast_result = run_forecast(repo, work, forecast, routes[0], sorted(found), sorted(declared), deps)

    selected = [d for d in deps if d["score"] >= min_score]
    if only:
        wanted = {canon(o) for o in only}
        selected = [d for d in deps if d["name"] in wanted]
    selected = selected[:max_deps]
    console.print("Selected for exit planning: %s" % ", ".join(d["name"] for d in selected))

    console.rule("5. Migration planning + verification")
    all_declared = set(declared)
    for dep in selected:
        console.print("[bold cyan]>> %s[/bold cyan] (risk %s, score %d)" % (dep["name"], dep["level"], dep["score"]))
        dep["attempts"] = []
        dep["ai"] = {"enabled": bool(gemma)}
        if not gemma:
            dep["verdict"] = "not_attempted"
            continue
        context = build_context(dep, repo, py_version, all_declared)
        try:
            proposal = _normalised(gemma.propose(context))
        except Exception as exc:  # network / quota / bad JSON: record honestly and continue
            dep["ai"]["error"] = str(exc)
            dep["verdict"] = "ai_failed"
            console.print("    [red]Gemma failed: %s[/red]" % exc)
            continue
        problems = migrate.validate(repo, proposal)
        if problems:
            console.print("    [yellow]proposal invalid (%d problems), asking Gemma to repair[/yellow]" % len(problems))
            dep["ai"]["validation_problems"] = problems
            try:
                proposal = _normalised(gemma.repair(context, proposal, {"validation_errors": problems}))
            except Exception as exc:
                dep["ai"]["error"] = str(exc)
        for n in range(1, attempts + 1):
            action = proposal["action"]
            console.print("    attempt %d: %s -> %s" % (n, action, proposal["replacement"].get("package") or "-"))
            if action == "keep":
                dep["attempts"].append({"n": n, "proposal": proposal, "skipped": "AI recommended keeping the dependency"})
                dep["verdict"] = "kept"
                break
            tree = migrate.copy_tree(repo, work / "candidates" / ("%s-%d" % (dep["name"], n)))
            applied = migrate.apply(tree, proposal)
            new_dists = [canon(Requirement(a["spec"]).name) for a in proposal["add_requirements"]]
            cand = verify_tree(tree, "%s-%d" % (dep["name"], n), work, python, routes,
                               modules=[], dists=new_dists) if not applied["errors"] else {}
            residual = residual_refs(tree, dep["modules"], dep["tokens"], action)
            comparison = compare(baseline, cand, dep["modules"], residual)
            v = verdict(comparison, action, applied)
            new_imports = {}
            for route, r in cand.items():
                for mod, res in ((r.get("smoke") or {}).get("imports") or {}).items():
                    new_imports.setdefault(mod, {})[route] = res
            dep["attempts"].append({
                "n": n, "proposal": proposal, "apply": applied, "verification": cand, "tree": str(tree),
                "comparison": comparison, "replacement_imports": new_imports, "verdict": v,
                "diff": migrate.diff_trees(repo, tree),
            })
            dep["verdict"] = v
            console.print("    verdict: [bold]%s[/bold]" % v)
            if v in PASSING or n == attempts:
                break
            problems = {"verdict": v, "apply_errors": applied["errors"], "comparison": comparison,
                        "test_output_tail": {r: (c.get("tests") or {}).get("output_tail", "") for r, c in cand.items()},
                        "install_output_tail": {r: c["install"]["steps"][-2]["output_tail"] if len(c["install"]["steps"]) > 1 else ""
                                                for r, c in cand.items()}}
            try:
                console.print("    [yellow]asking Gemma to repair using the observed failures[/yellow]")
                proposal = _normalised(gemma.repair(context, proposal, problems))
            except Exception as exc:
                dep["ai"]["error"] = str(exc)
                break
        last = dep["attempts"][-1] if dep["attempts"] else None
        if last and "comparison" in last:
            evidence = {"dependency": dep["name"], "action": last["proposal"]["action"],
                        "replacement": last["proposal"]["replacement"], "verdict": last["verdict"],
                        "comparison": last["comparison"], "apply_log": last["apply"],
                        "replacement_imports": last["replacement_imports"],
                        "risk_indicators": [i["detail"] for i in dep["indicators"]]}
            try:
                dep["ai"]["review"] = gemma.review(evidence)
            except Exception as exc:
                dep["ai"]["review_error"] = str(exc)

    combined = None
    passing = [d for d in selected if d.get("verdict") in PASSING]
    if len(passing) > 1:
        console.rule("6. Combined migration (all verified exits applied together)")
        tree = migrate.copy_tree(repo, work / "candidates" / "combined")
        logs = []
        for d in passing:
            logs.append({"dependency": d["name"], **migrate.apply(tree, d["attempts"][-1]["proposal"])})
        cand = verify_tree(tree, "combined", work, python, routes)
        comparison = compare(baseline, cand, [], [])
        combined = {"dependencies": [d["name"] for d in passing], "apply": logs, "verification": cand, "tree": str(tree),
                    "comparison": comparison, "diff": migrate.diff_trees(repo, tree),
                    "verdict": verdict(comparison, "upgrade", {"errors": [e for l in logs for e in l["errors"]]})}
        console.print("combined verdict: [bold]%s[/bold]" % combined["verdict"])

    organized = None
    if organize:
        base_tree, base_results = repo, baseline
        if combined and combined["verdict"] in PASSING:
            base_tree, base_results = Path(combined["tree"]), combined["verification"]
        elif len(passing) == 1:
            base_tree, base_results = Path(passing[0]["attempts"][-1]["tree"]), passing[0]["attempts"][-1]["verification"]
        maps = [r.get("smoke") or {} for r in base_results.values()] + [r.get("smoke") or {} for r in baseline.values()]
        all_pkg_map, all_installed = {}, {}
        for sm in maps:
            for k, v in sm.get("pkg_map", {}).items():
                all_pkg_map.setdefault(k, v)
            for k, v in sm.get("dists", {}).items():
                all_installed.setdefault(k, v)
        organized = run_organizer(base_tree, base_results, work, python, routes, all_pkg_map, all_installed, gemma)

    console.rule("7. Writing Dependency Exit Capsules")
    run_info = {
        "tool": "depexit %s" % __version__,
        "started": started.isoformat(timespec="seconds"),
        "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **target_info,
        "python": py_version,
        "install_routes": routes,
        "ai_model": gemma.model if gemma else None,
        "ai_calls": len(gemma.calls) if gemma else 0,
        "manifests": man,
        "parse_errors": parse_errors,
    }
    paths = capsule.write_all(out_dir, run_info, baseline, deps, selected, combined, gemma.calls if gemma else [],
                              organized=organized, forecast=forecast_result)
    console.print("Capsules written to [bold]%s[/bold] (open %s)" % (out_dir, paths["index"]))
    return paths


def _normalised(p):
    from .gemma import normalise
    return normalise(p)


def _print_risk_table(deps):
    table = Table(title="Dependency risk (observed)")
    for col in ("Dependency", "Level", "Score", "Indicators"):
        table.add_column(col)
    colors = {"high": "red", "medium": "yellow", "low": "green", "none": "dim"}
    for d in deps:
        table.add_row(d["name"], "[%s]%s[/]" % (colors[d["level"]], d["level"]), str(d["score"]),
                      ", ".join(sorted({i["id"] for i in d["indicators"]})) or "-")
    console.print(table)


def dumps(obj):
    return json.dumps(obj, indent=2, default=str)
