# DepExit report

**Target:** https://github.com/rishrek/LinkWatch @ `f52f3930c8`  
**Python:** 3.12.7 · **Install routes:** requirements, pyproject · **AI:** gemma-4-26b-a4b-it (3 calls)  
**Run:** 2026-10-15T09:52:16+00:00 → 2026-10-15T09:59:05+00:00

## Baseline 🔍 OBSERVED

| Route | Install | Tests | Failing |
|---|---|---|---|
| requirements | ok | 9 passed 1 failed | `tests.test_report::test_report_renders_all_sites` |
| pyproject | ok | 9 passed 1 failed | `tests.test_report::test_report_renders_all_sites` |

## All dependencies 🔍 OBSERVED

| Dependency | Risk | Score | Indicators | Exit capsule | Verdict |
|---|---|---|---|---|---|
| `jinja2` | high | 8 | import_fails, pinned_behind, test_failure | [capsule](dependencies/jinja2.md) | ✅ verified_improvement |
| `setuptools` | high | 8 | deprecation_warning, pinned_behind, tooling_as_runtime | - | ➖  |
| `nose` | high | 6 | import_fails, stale | - | ➖  |
| `pydantic` | high | 5 | manifest_conflict, pinned_behind | - | ➖  |
| `requests` | high | 5 | monkeypatch, pinned_behind, private_api | - | ➖  |
| `idna` | medium | 4 | pinned_behind, unused | - | ➖  |
| `appdirs` | high | 3 | stale | - | ➖  |
| `toml` | high | 3 | stale | - | ➖  |
| `certifi` | medium | 2 | undeclared | - | ➖  |
| `chardet` | medium | 2 | pinned_behind | - | ➖  |
| `python-dateutil` | medium | 2 | stale | - | ➖  |
| `six` | medium | 2 | unused | - | ➖  |
| `click` | none | 0 | - | - | ➖  |
| `pytest` | none | 0 | - | - | ➖  |
| `rich` | none | 0 | - | - | ➖  |

## Auto-Organizer

Applied on top of the verified migration. 
Manifests rebuilt from the observed evidence, then installed and tested: **✅ verified_no_regression**

| Action | Dependency | Reason (🔍 observed) | Evidence |
|---|---|---|---|
| declare | `certifi` | imported by the code but not declared (was only installed transitively) | linkwatch/checker.py:4; installed transitively as 2026.7.22 |
| remove | `idna` | declared as runtime but never imported or referenced | Declared as a runtime dependency but never imported or referenced |
| flag_conflict | `pydantic` | requirements.txt and pyproject.toml disagree; left for the migration step | pydantic==1.10.13 vs pydantic>=2 |
| remove | `six` | declared as runtime but never imported or referenced | Declared as a runtime dependency but never imported or referenced |

Requirement comments marked `(AI)` were written by Gemma 4.

Patch: [`organizer.patch`](organizer.patch)

---
Files: `capsule.json` (index), `dependencies/*.json|md|patch` (one capsule per dependency), `ai_calls.json` (every Gemma call with prompt hash and raw response).
