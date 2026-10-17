"""Gemma 4 integration (via the Gemini API / Google AI Studio).

Gemma is used for three jobs, every output is labelled as AI-generated:
  1. propose  - choose a replacement and write the concrete migration (code + manifest edits)
  2. revise   - when verification fails, read the real test failures and repair its proposal
  3. review   - interpret the observed before/after evidence into gaps and open concerns
"""
import hashlib
import json
import os
import re
import time
from pathlib import Path

DEFAULT_MODEL = "gemma-4-26b-a4b-it"

SYSTEM = """You are DepExit, a careful senior Python maintainer. You help migrate a project away
from a risky third-party dependency. You are given OBSERVED facts (from manifests, PyPI, static
analysis and real test runs) and the full source of the files that use the dependency.

Reply with ONE JSON object and nothing else (no markdown fences). Schema:
{
  "assessment": "2-3 sentences explaining the risk, grounded only in the observed evidence",
  "action": "replace | upgrade | remove | declare | keep",
  "replacement": {"package": "dist name or null", "requirement": "pip spec or null", "why": "short reason"},
  "remove_requirements": ["dist names to delete from every manifest"],
  "add_requirements": [{"spec": "pip requirement e.g. platformdirs>=4", "group": "runtime | dev"}],
  "code_changes": [{"file": "relative/path", "find": "exact text from the file", "replace": "new text", "why": "short"}],
  "migration_steps": ["ordered steps a maintainer would follow"],
  "suggested_tests": ["specific tests or checks that would prove the migration is safe"],
  "risks": ["behaviour differences or compatibility concerns to watch"]
}
Rules:
- "find" must be copied EXACTLY (including indentation) from the provided file contents and must
  occur exactly once in that file. Include enough surrounding lines to make it unique.
- Make the smallest change that removes the risk. Do not refactor unrelated code.
- Prefer the standard library or actively maintained packages. Never invent APIs.
- For a non-Python file such as a Makefile, edit it the same way with find/replace.
- "upgrade" = keep the package with a new version range; "declare" = add a missing declaration;
  "remove" = drop an unused/dead package; "keep" = no safe change (explain in risks).
- Respect the target Python version.
"""

REVIEW_SYSTEM = """You are reviewing the result of an automated dependency migration. You get the
observed before/after evidence from real installs and test runs. Reply with ONE JSON object only:
{
  "summary": "2-3 sentences",
  "compatibility_gaps": ["concrete gaps visible in the evidence or likely behavioural differences"],
  "unresolved_risks": ["what is still not proven"],
  "additional_tests": ["tests a maintainer should add to close the gaps"],
  "confidence": "low | medium | high",
  "confidence_reason": "one sentence"
}
Only state something as a fact if it appears in the evidence; otherwise phrase it as a possibility.
"""


class GemmaError(RuntimeError):
    pass


class Gemma:
    def __init__(self, model=None, log_dir=None):
        from google import genai  # imported lazily so --no-ai works without the SDK

        key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise GemmaError("Set GEMINI_API_KEY (free key from https://aistudio.google.com/apikey)")
        base_url = os.environ.get("GEMINI_BASE_URL")  # only for offline testing / proxies
        if base_url:
            from google.genai import types
            self.client = genai.Client(api_key=key, http_options=types.HttpOptions(base_url=base_url))
        else:
            self.client = genai.Client(api_key=key)
        self.model = model or os.environ.get("GEMMA_MODEL", DEFAULT_MODEL)
        self.calls = []
        self.log_dir = Path(log_dir) if log_dir else None
        self._plain = False  # fallback when system instructions / JSON mode are not supported

    # ---------------------------------------------------------------- core
    def _generate(self, system, prompt, purpose):
        from google.genai import errors, types

        attempt, delay = 0, 8
        while True:
            attempt += 1
            started = time.monotonic()
            try:
                if self._plain:
                    resp = self.client.models.generate_content(
                        model=self.model, contents=system + "\n\n" + prompt,
                        config=types.GenerateContentConfig(temperature=0.2))
                else:
                    resp = self.client.models.generate_content(
                        model=self.model, contents=prompt,
                        config=types.GenerateContentConfig(system_instruction=system, temperature=0.2,
                                                           response_mime_type="application/json"))
                text = resp.text or ""
                break
            except errors.ClientError as exc:
                code = getattr(exc, "code", None)
                if code == 400 and not self._plain:
                    self._plain = True  # model rejected system_instruction or JSON mode
                    continue
                if code == 429 and attempt < 4:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise GemmaError("Gemma API error: %s" % exc) from exc
            except errors.ServerError as exc:
                if attempt < 4:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise GemmaError("Gemma API error: %s" % exc) from exc
        self.calls.append({
            "purpose": purpose,
            "model": self.model,
            "prompt_sha256": hashlib.sha256((system + prompt).encode()).hexdigest()[:16],
            "prompt_chars": len(system) + len(prompt),
            "response_chars": len(text),
            "seconds": round(time.monotonic() - started, 1),
            "raw_response": text[:6000],
        })
        return text

    def _json(self, system, prompt, purpose):
        text = self._generate(system, prompt, purpose)
        try:
            return parse_json(text)
        except ValueError as exc:
            fix = (prompt + "\n\nYour previous reply was not valid JSON (%s). Reply again with ONLY the JSON object." % exc)
            return parse_json(self._generate(system, fix, purpose + ":json-retry"))

    # ---------------------------------------------------------------- jobs
    def propose(self, context):
        prompt = "Migrate away from this dependency.\n\nOBSERVED CONTEXT (JSON):\n" + json.dumps(context, indent=1)
        return self._json(SYSTEM, prompt, "propose:" + context["dependency"]["name"])

    def repair(self, context, proposal, problems):
        prompt = ("Your previous proposal could not be applied or failed verification.\n\nOBSERVED CONTEXT (JSON):\n"
                  + json.dumps(context, indent=1)
                  + "\n\nYOUR PREVIOUS PROPOSAL:\n" + json.dumps(_clean(proposal), indent=1)
                  + "\n\nOBSERVED PROBLEMS:\n" + json.dumps(problems, indent=1)
                  + "\n\nReturn a corrected, complete proposal in the same JSON schema.")
        return self._json(SYSTEM, prompt, "repair:" + context["dependency"]["name"])

    def review(self, evidence):
        prompt = "EVIDENCE (JSON):\n" + json.dumps(evidence, indent=1)
        return self._json(REVIEW_SYSTEM, prompt, "review:" + evidence["dependency"])


def parse_json(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(text[start:end + 1])


def _clean(p):
    return {k: v for k, v in (p or {}).items() if not k.startswith("_")}


def normalise(p):
    """Make a model reply safe to consume: right types, no surprises."""
    p = dict(p or {})
    p["action"] = str(p.get("action") or "keep").lower().strip()
    p["replacement"] = p.get("replacement") if isinstance(p.get("replacement"), dict) else {}
    for key in ("remove_requirements", "migration_steps", "suggested_tests", "risks"):
        val = p.get(key) or []
        p[key] = [str(v) for v in val] if isinstance(val, list) else [str(val)]
    adds = []
    for a in p.get("add_requirements") or []:
        if isinstance(a, str):
            a = {"spec": a, "group": "runtime"}
        if isinstance(a, dict) and a.get("spec"):
            adds.append({"spec": str(a["spec"]), "group": "dev" if str(a.get("group")) == "dev" else "runtime"})
    p["add_requirements"] = adds
    changes = []
    for c in p.get("code_changes") or []:
        if isinstance(c, dict) and c.get("file") and "find" in c and "replace" in c:
            fname = str(c["file"]).replace("\\", "/")
            changes.append({"file": fname[2:] if fname.startswith("./") else fname,
                            "find": str(c["find"]), "replace": str(c["replace"]), "why": str(c.get("why", ""))})
    p["code_changes"] = changes
    p["assessment"] = str(p.get("assessment", ""))
    return p
