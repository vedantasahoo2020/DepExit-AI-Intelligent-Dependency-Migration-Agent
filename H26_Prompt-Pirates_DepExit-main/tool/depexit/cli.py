import argparse
import sys

from . import __version__


def main(argv=None):
    p = argparse.ArgumentParser(prog="depexit", description="AI-assisted dependency exit planning, verified by execution.")
    p.add_argument("--version", action="version", version="depexit " + __version__)
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("analyze", help="scan a repository and build Dependency Exit Capsules")
    a.add_argument("target", help="git URL or local path of the repository to analyse")
    a.add_argument("--out", default="capsule", help="output directory (default: capsule)")
    a.add_argument("--python", default="3.12", help="Python version or interpreter for the test envs (default 3.12)")
    a.add_argument("--only", help="comma-separated dependency names to plan exits for")
    a.add_argument("--max-deps", type=int, default=20, help="max dependencies to migrate (default 20)")
    a.add_argument("--min-score", type=int, default=2, help="minimum risk score to plan an exit (default 2)")
    a.add_argument("--attempts", type=int, default=2, help="verify/repair rounds per dependency (default 2)")
    a.add_argument("--model", help="Gemma model id (default gemma-4-26b-a4b-it, or $GEMMA_MODEL)")
    a.add_argument("--no-ai", action="store_true", help="observed evidence only, no Gemma calls")
    a.add_argument("--work", default=".depexit-work", help="scratch directory for clones and venvs")
    a.add_argument("--forecast", help="extra Python versions to forecast, e.g. 3.13,3.14")
    a.add_argument("--no-organize", action="store_true", help="skip the Auto-Organizer")
    b = sub.add_parser("branch", help="commit verified patches + capsules to a new branch in your local clone")
    b.add_argument("repo", help="path to your local git clone of the target repository")
    b.add_argument("--capsule", default="capsule", help="capsule directory from `depexit analyze`")
    b.add_argument("--name", help="branch name (default depexit/exit-<timestamp>)")
    b.add_argument("--skip-organizer", action="store_true", help="only apply the dependency migrations")
    w = sub.add_parser("web", help="start the local web dashboard")
    w.add_argument("--port", type=int, default=8000)
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    args = p.parse_args(argv)
    from .util import ensure_git_on_path
    ensure_git_on_path()

    if args.command == "branch":
        from .gitbranch import create_branch
        which = ("migration",) if args.skip_organizer else ("migration", "organizer")
        res = create_branch(args.repo, args.capsule, which, args.name)
        for s in res["steps"]:
            print("%s %s" % ("ok " if s["returncode"] == 0 else "ERR", s["cmd"]))
        print(("Created branch %s (%s). Next: %s" % (res["branch"], ", ".join(res["applied"]), res["next"]))
              if res["ok"] else "Failed: %s" % res.get("error", "nothing applied"))
        return 0 if res["ok"] else 1
    if args.command == "web":
        _load_dotenv()
        from .web import serve
        serve(args.host, args.port, open_browser=not args.no_browser)
        return 0

    if args.command == "analyze":
        _load_dotenv()
        from .pipeline import analyze
        analyze(args.target, out_dir=args.out, python=args.python, use_ai=not args.no_ai,
                only=args.only.split(",") if args.only else None, max_deps=args.max_deps,
                attempts=max(1, args.attempts), min_score=args.min_score, work=args.work, model=args.model,
                forecast=[v.strip() for v in args.forecast.split(",")] if args.forecast else None,
                organize=not args.no_organize)
    return 0


def _load_dotenv():
    """Read KEY=VALUE lines from .env so the API key never has to be committed."""
    import os
    from pathlib import Path

    path = Path(".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


if __name__ == "__main__":
    sys.exit(main())
