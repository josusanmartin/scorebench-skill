#!/usr/bin/env python3
"""Repair retained OpenRouter receipts without starting inference or changing lifecycle."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sys

from openrouter_generations import lookup_generation
from openrouter_journal import reconcile_requests


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--check", action="store_true", help="query metadata and preview repairs without writing evidence")
    args = parser.parse_args(argv)
    log = Path(args.workspace).resolve() / ".scorebench/openrouter/usage.jsonl"
    if not log.is_file():
        parser.error("no retained OpenRouter usage ledger in this workspace")
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        parser.error("OPENROUTER_API_KEY is required; do not pass it on the command line")
    try:
        # --check must not create a lock file. Every supported launcher creates it.
        with open(str(log) + ".lock", "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            report = reconcile_requests(log, lambda generation_id, model: lookup_generation(
                generation_id, model, upstream=os.environ.get("OPENROUTER_BASE", "https://openrouter.ai"),
                api_key=key, require_final=True), check=args.check, abandoned=True)
        print(json.dumps({**report, "inference_started": False, "lifecycle_changed": False}))
        return 0 if report["accounting_complete"] else 1
    except BlockingIOError:
        print("OpenRouter ledger belongs to a running worker; its supervisor owns reconciliation", file=sys.stderr)
    except Exception as exc:
        print(f"OpenRouter receipt reconciliation refused ({type(exc).__name__}); preserve the evidence", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
