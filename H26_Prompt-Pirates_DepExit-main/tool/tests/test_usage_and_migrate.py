import textwrap

from depexit import manifest, migrate, risk, usage
from depexit.gemma import normalise, parse_json


def make_repo(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "net.py").write_text(textwrap.dedent("""\
        import requests
        from requests import Session as S
        from requests.utils import _private_helper

        _orig = requests.Session.request
        requests.Session.request = lambda *a, **k: None

        def get(url):
            from appdirs import user_cache_dir
            return S().get(url), user_cache_dir("x")
        """))
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\nappdirs==1.4.4\n# PyYAML==5.1\n")
    (tmp_path / "requirements-dev.txt").write_text("nose==1.3.7\n")
    (tmp_path / "pyproject.toml").write_text(textwrap.dedent("""\
        [project]
        name = "demo"
        version = "0"
        dependencies = [
            "requests==2.31.0",
            "appdirs==1.4.4",
        ]
        """))
    (tmp_path / "Makefile").write_text("test-nose:\n\tnosetests tests/\n")
    return tmp_path


def test_scan_finds_aliases_lazy_private_and_monkeypatch(tmp_path):
    repo = make_repo(tmp_path)
    found, errors = usage.scan(repo)
    assert not errors
    req = found["requests"]
    kinds = {u["kind"] for u in req["usages"]}
    assert "monkeypatch" in kinds and "call" in kinds
    assert any(u["expr"] == "S" for u in req["usages"])  # alias resolved
    assert any(i["private_api"] for i in req["imports"])
    lazy = found["appdirs"]["imports"][0]
    assert lazy["lazy"] and lazy["scope"] == "get"


def test_text_references_find_console_scripts(tmp_path):
    repo = make_repo(tmp_path)
    hits = usage.text_references(repo, ["nose", "nosetests"])
    assert hits and hits[0]["file"] == "Makefile"


def test_manifest_load_and_groups(tmp_path):
    repo = make_repo(tmp_path)
    m = manifest.load(repo)
    by = manifest.by_name(m)
    assert {e["file"] for e in by["appdirs"]} == {"requirements.txt", "pyproject.toml"}
    assert by["nose"][0]["group"] == "dev"
    assert m["commented_out"][0]["text"] == "# PyYAML==5.1"


def test_apply_replaces_requirements_and_code(tmp_path):
    repo = make_repo(tmp_path)
    proposal = normalise({
        "action": "replace",
        "remove_requirements": ["appdirs"],
        "add_requirements": [{"spec": "platformdirs>=4", "group": "runtime"}, "requests>=2.32"],
        "code_changes": [{"file": "pkg/net.py", "find": "from appdirs import user_cache_dir",
                          "replace": "from platformdirs import user_cache_dir"}],
    })
    assert migrate.validate(repo, proposal) == []
    result = migrate.apply(repo, proposal)
    assert result["errors"] == []
    reqs = (repo / "requirements.txt").read_text()
    assert "appdirs" not in reqs and "platformdirs>=4" in reqs and "requests>=2.32" in reqs
    py = (repo / "pyproject.toml").read_text()
    assert '"platformdirs>=4",' in py and "appdirs" not in py and '"requests>=2.32",' in py
    assert "platformdirs" in (repo / "pkg" / "net.py").read_text()


def test_validate_rejects_ambiguous_or_escaping_changes(tmp_path):
    repo = make_repo(tmp_path)
    proposal = normalise({"code_changes": [
        {"file": "pkg/net.py", "find": "requests", "replace": "x"},
        {"file": "../outside.py", "find": "a", "replace": "b"},
    ]})
    problems = migrate.validate(repo, proposal)
    assert len(problems) == 2


def test_parse_json_tolerates_fences():
    assert parse_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_risk_flags_undeclared_and_conflict():
    meta = {"latest_version": "2.0", "latest_release_date": "2015-01-01T00:00:00Z", "dev_status": [],
            "_versions": ["1.0", "2.0"], "_first_upload": lambda v: None, "_yanked": lambda v: False}
    decl = [{"name": "x", "group": "runtime", "specifier": "==1.0", "pinned": "1.0", "file": "requirements.txt", "line": 1},
            {"name": "x", "group": "runtime", "specifier": ">=2", "pinned": None, "file": "pyproject.toml", "line": 3}]
    ind = risk.assess("x", decl, meta, {"imports": [{"file": "a.py", "line": 1, "private_api": False}], "usages": []}, [], {}, ["x"])
    ids = {i["id"] for i in ind}
    assert {"manifest_conflict", "stale", "pinned_behind"} <= ids
    und = risk.assess("y", [], meta, {"imports": [{"file": "a.py", "line": 1, "private_api": False}], "usages": []}, [], {}, ["y"])
    assert "undeclared" in {i["id"] for i in und}


def test_organizer_removes_unused_declares_missing_and_syncs(tmp_path):
    from depexit import organizer

    repo = make_repo(tmp_path)
    (repo / "requirements.txt").write_text("six\nrequests==2.31.0\nappdirs==1.4.4\n# PyYAML==5.1\n")
    m = manifest.load(repo)
    by = manifest.by_name(m)
    found, _ = usage.scan(repo)

    def dep(name, mod, inds):
        return {"name": name, "declarations": by.get(name, []), "usage": found.get(mod, {"imports": [], "usages": []}),
                "text_references": [], "installed_version": "1.0", "pypi": {},
                "indicators": [{"id": i, "detail": i} for i in inds]}

    deps = [dep("six", "six", ["unused"]), dep("requests", "requests", []), dep("appdirs", "appdirs", []),
            dep("certifi", "certifi", ["undeclared"])]
    deps[3]["usage"] = {"imports": [{"file": "pkg/net.py", "line": 1}], "usages": []}
    plan = organizer.plan(deps)
    acts = {(a["action"], a["dependency"]) for a in plan["actions"]}
    assert ("remove", "six") in acts and ("declare", "certifi") in acts
    organizer.apply(repo, plan, m["commented_out"], {"requests": "HTTP calls"})
    reqs = (repo / "requirements.txt").read_text().splitlines()
    body = [l for l in reqs if l and not l.startswith("#")]
    assert body == sorted(body, key=str.lower)
    assert "six" not in reqs and "certifi" in reqs and "# PyYAML==5.1" in reqs
    assert "requests==2.31.0  # (AI) HTTP calls" in reqs
    py = (repo / "pyproject.toml").read_text()
    assert '"certifi",' in py and '"appdirs==1.4.4",' in py


def test_relocate_tolerates_whitespace_and_reindents():
    text = "def f():\n    x = 1\n    return x\n"
    ch = {"file": "a.py", "find": "x = 1\nreturn x", "replace": "y = 2\nreturn y"}
    assert migrate.relocate(text, ch)
    assert text.replace(ch["find"], ch["replace"]) == "def f():\n    y = 2\n    return y\n"
    ambiguous = {"file": "a.py", "find": "pass", "replace": "x"}
    assert not migrate.relocate("pass\npass\n", ambiguous)
