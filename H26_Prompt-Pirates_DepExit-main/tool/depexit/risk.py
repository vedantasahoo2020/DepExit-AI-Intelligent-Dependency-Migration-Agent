"""Turn observed facts into risk indicators. No AI is used here."""
import re
from datetime import datetime, timezone

from packaging.version import InvalidVersion, Version

from . import pypi
from .env import normalise_path_text

TOOLING = {"setuptools", "pip", "wheel"}


def _age_years(iso):
    if not iso:
        return None
    try:
        when = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).days / 365.25


def _ind(code, severity, detail, evidence, route=None):
    evidence = normalise_path_text(evidence) if isinstance(evidence, str) else evidence
    return {"id": code, "severity": severity, "detail": detail, "evidence": evidence, "source": "observed",
            "install_routes": [route] if route else []}


def _mentions(text, module):
    """True if a warning/traceback names this module (not just a substring like 'py' in '.py')."""
    text = text.replace("\\", "/")
    if "site-packages/%s/" % module in text or "site-packages/%s.py" % module in text:
        return True
    return re.search(r"(?<![\w./-])%s(?![\w/.-])" % re.escape(module), text) is not None


def assess(name, decl, meta, usage, text_refs, baseline, modules):
    """Return a list of indicators for one distribution.

    decl      manifest entries for the dist ([] if undeclared)
    meta      PyPI metadata
    usage     {"imports": [...], "usages": [...]} merged over its modules
    baseline  {route: {"smoke": ..., "tests": ...}}
    modules   import names that belong to the dist
    """
    out = []
    runtime_decl = [d for d in decl if d["group"] in ("runtime",) or d["group"].startswith("extra:")]

    # --- execution evidence -------------------------------------------------
    for route, base in baseline.items():
        for mod, res in (base.get("smoke", {}).get("imports") or {}).items():
            if mod in modules and not res["ok"]:
                out.append(_ind("import_fails", 3,
                                "`import %s` fails on Python %s" % (mod, base["smoke"].get("python")),
                                res["error"], route))
            elif mod in modules:
                for w in res.get("warnings", []):
                    if "Deprecat" in w:
                        out.append(_ind("deprecation_warning", 2, "Importing `%s` emits a deprecation warning" % mod, w, route))
        failing = [(tid, t) for tid, t in (base.get("tests", {}).get("tests") or {}).items()
                   if t["outcome"] in ("failed", "error")]
        for tid, t in failing:
            msg = t["message"].replace("\\", "/")
            if any(("site-packages/%s/" % m) in msg or ("module '%s" % m) in msg or ("from '%s'" % m) in msg
                   or ("'%s'" % m) in msg.split("\n")[0] for m in modules):
                out.append(_ind("test_failure", 3, "Test `%s` fails inside this dependency" % tid,
                                msg.split("\n")[0][:300], route))
        for w in base.get("tests", {}).get("warnings", []):
            if "Deprecat" in w and any(_mentions(w, m) for m in modules):
                out.append(_ind("deprecation_warning", 2, "Deprecation warning during the test run",
                                w, route))

    # --- manifest evidence -------------------------------------------------
    if not decl and usage["imports"]:
        out.append(_ind("undeclared", 2, "Imported by the code but not declared in any manifest "
                        "(only installed transitively)",
                        "; ".join("%s:%s" % (i["file"], i["line"]) for i in usage["imports"][:5])))
    if decl and not usage["imports"] and not text_refs:
        groups = sorted({d["group"] for d in decl})
        if any(g == "runtime" for g in groups):
            out.append(_ind("unused", 2, "Declared as a runtime dependency but never imported or referenced",
                            ", ".join("%s:%s" % (d["file"], d["line"]) for d in decl)))
    specs = sorted({d["specifier"] for d in decl if d["specifier"]})
    if len(specs) > 1:
        ok = pypi.satisfiable(meta, specs)
        where = ", ".join("%s says `%s`" % (d["file"], d["specifier"] or "any") for d in decl)
        if ok is False:
            out.append(_ind("manifest_conflict", 3,
                            "Manifests disagree and no released version satisfies all of them", where))
        else:
            out.append(_ind("manifest_mismatch", 1, "Manifests declare different version ranges", where))
    if name in TOOLING and runtime_decl:
        out.append(_ind("tooling_as_runtime", 2, "Packaging/build tool declared as a runtime dependency",
                        ", ".join("%s:%s" % (d["file"], d["line"]) for d in runtime_decl)))

    # --- ecosystem evidence (PyPI) -----------------------------------------
    if meta and "error" not in meta:
        age = _age_years(meta.get("latest_release_date"))
        if age is not None and age >= 2:
            out.append(_ind("stale", 3 if age >= 4 else 2,
                            "Latest release (%s) is %.1f years old" % (meta["latest_version"], age),
                            meta.get("latest_release_date")))
        if any("Inactive" in s for s in meta.get("dev_status", [])):
            out.append(_ind("inactive", 3, "PyPI classifier marks the project inactive", ", ".join(meta["dev_status"])))
        pins = sorted({d["pinned"] for d in decl if d["pinned"]})
        for pin in pins:
            try:
                pv, lv = Version(pin), Version(meta["latest_version"])
            except (InvalidVersion, TypeError):
                continue
            if pv.major < lv.major:
                pin_age = _age_years(pypi.release_date(meta, pin))
                out.append(_ind("pinned_behind", 2,
                                "Pinned to %s; latest is %s (%d major version(s) behind)" % (pin, lv, lv.major - pv.major),
                                "pinned release date: %s%s" % (pypi.release_date(meta, pin),
                                                                "" if pin_age is None else " (%.1f years old)" % pin_age)))
            elif pv < lv and (lv.minor - pv.minor >= 3 or pv.major < lv.major):
                out.append(_ind("pinned_behind", 1, "Pinned to %s; latest is %s" % (pin, lv), ""))
            if pypi.is_yanked(meta, pin):
                out.append(_ind("yanked_pin", 3, "Pinned version %s was yanked from PyPI" % pin, ""))
    elif meta and "error" in meta:
        out.append(_ind("metadata_unavailable", 1, "Could not read PyPI metadata", meta["error"]))

    # --- code evidence ------------------------------------------------------
    priv = [u for u in usage["usages"] if u["private_api"]] + [i for i in usage["imports"] if i["private_api"]]
    if priv:
        out.append(_ind("private_api", 2, "Code relies on private (underscore) API of this package",
                        "; ".join("%s:%s `%s`" % (p["file"], p["line"], p.get("symbol") or p.get("statement")) for p in priv[:4])))
    patches = [u for u in usage["usages"] if u["kind"] == "monkeypatch"]
    if patches:
        out.append(_ind("monkeypatch", 2, "Code monkeypatches this library at import time",
                        "; ".join("%s:%s `%s`" % (p["file"], p["line"], p["code"]) for p in patches[:3])))
    return _dedupe(out)


def _dedupe(items):
    """Merge identical findings seen on several install routes."""
    merged = {}
    for i in items:
        key = (i["id"], i["detail"], i["evidence"])
        if key in merged:
            merged[key]["install_routes"] = sorted(set(merged[key]["install_routes"]) | set(i["install_routes"]))
        else:
            merged[key] = i
    return list(merged.values())


def score(indicators):
    return sum(i["severity"] for i in indicators)


def level(indicators):
    s = score(indicators)
    if s >= 5 or any(i["severity"] >= 3 for i in indicators):
        return "high"
    if s >= 2:
        return "medium"
    return "low" if s else "none"
