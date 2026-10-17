import os
import re
import subprocess
import time
from pathlib import Path


def canon(name):
    """PEP 503 normalised distribution name."""
    return re.sub(r"[-_.]+", "-", name).lower().strip()


def run(cmd, cwd=None, timeout=900, env=None):
    """Run a command and capture everything we need as evidence."""
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [str(c) for c in cmd], cwd=cwd, capture_output=True, text=True,
            timeout=timeout, env=env, encoding="utf-8", errors="replace",
        )
        code, out, err = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        code, out, err = -1, exc.stdout or "", "TIMEOUT after %ss" % timeout
    except FileNotFoundError as exc:
        code, out, err = -2, "", str(exc)
    return {
        "cmd": " ".join(str(c) for c in cmd),
        "returncode": code,
        "stdout": out if isinstance(out, str) else out.decode("utf-8", "replace"),
        "stderr": err if isinstance(err, str) else err.decode("utf-8", "replace"),
        "seconds": round(time.monotonic() - started, 1),
    }


def tail(text, n=40):
    lines = (text or "").strip().splitlines()
    return "\n".join(lines[-n:])


def venv_python(env_dir):
    env_dir = Path(env_dir)
    if os.name == "nt":
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


SKIP_DIRS = {
    ".git", ".hg", ".venv", "venv", "env", ".env", "build", "dist", "__pycache__",
    ".tox", ".nox", ".mypy_cache", ".pytest_cache", "node_modules", ".depexit-work",
    "site-packages",
}


def skipped(rel_parts):
    return any(p in SKIP_DIRS or p.startswith(".venv") or p.endswith(".egg-info")
               for p in rel_parts)


def ensure_git_on_path():
    """Windows: a freshly installed Git is often missing from PATH until a new terminal is opened.
    Look in the usual install folders so DepExit works anyway. Returns True if git is available."""
    import shutil

    if shutil.which("git"):
        return True
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "cmd",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Git" / "cmd",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Git" / "cmd",
    ]
    for folder in candidates:
        if (folder / "git.exe").exists() or (folder / "git").exists():
            os.environ["PATH"] = str(folder) + os.pathsep + os.environ.get("PATH", "")
            return True
    return False
