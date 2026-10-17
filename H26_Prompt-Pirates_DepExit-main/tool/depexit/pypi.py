"""Live package metadata from the PyPI JSON API (observed, not AI)."""
import json
import urllib.error
import urllib.request

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

_CACHE = {}


def fetch(name):
    if name in _CACHE:
        return _CACHE[name]
    url = "https://pypi.org/pypi/%s/json" % name
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "depexit/0.1"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        _CACHE[name] = {"error": "PyPI lookup failed: %s" % exc, "source_url": url}
        return _CACHE[name]

    info = data.get("info", {})
    releases = data.get("releases", {})

    def first_upload(version):
        files = releases.get(version) or []
        times = [f.get("upload_time_iso_8601") or f.get("upload_time") for f in files]
        times = [t for t in times if t]
        return min(times) if times else None

    def yanked(version):
        files = releases.get(version) or []
        return bool(files) and all(f.get("yanked") for f in files)

    stable = []
    for v, files in releases.items():
        if not files:
            continue
        try:
            pv = Version(v)
        except InvalidVersion:
            continue
        if not pv.is_prerelease:
            stable.append(pv)
    stable.sort()
    classifiers = info.get("classifiers") or []
    result = {
        "source_url": url,
        "summary": info.get("summary"),
        "latest_version": info.get("version"),
        "latest_release_date": first_upload(info.get("version")),
        "requires_python_latest": info.get("requires_python"),
        "dev_status": [c.split("::")[-1].strip() for c in classifiers if c.startswith("Development Status")],
        "python_classifiers": sorted({c.split("::")[-1].strip() for c in classifiers
                                      if c.startswith("Programming Language :: Python :: 3.")}),
        "project_urls": info.get("project_urls") or {},
        "license": info.get("license_expression") or (info.get("license") or "")[:80],
        "release_count": len(stable),
        "_versions": [str(v) for v in stable],
        "_first_upload": first_upload,
        "_yanked": yanked,
    }
    _CACHE[name] = result
    return result


def release_date(meta, version):
    if not meta or "error" in meta or not version:
        return None
    return meta["_first_upload"](version)


def is_yanked(meta, version):
    if not meta or "error" in meta or not version:
        return False
    return meta["_yanked"](version)


def satisfiable(meta, specifiers):
    """Is there any released version that satisfies every specifier at once?"""
    if not meta or "error" in meta:
        return None
    sets = [SpecifierSet(s) for s in specifiers]
    for v in meta["_versions"]:
        if all(s.contains(v, prereleases=False) for s in sets):
            return True
    return False


def public(meta):
    return {k: v for k, v in (meta or {}).items() if not k.startswith("_")}
