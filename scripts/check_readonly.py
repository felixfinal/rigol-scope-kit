"""Explicit live P1 acceptance: reads only, never changes instrument settings."""
import argparse
import json
from pathlib import Path

from rigol_scope.config import Settings
from rigol_scope.service import ScopeService

parser = argparse.ArgumentParser()
parser.add_argument("--config", type=Path, required=True)
parser.add_argument("--device", default="bench-scope")
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
service = ScopeService(Settings.load(args.config))
args.output.mkdir(parents=True, exist_ok=True)
for action in ("identify", "capabilities", "status", "generator_status", "screenshot"):
    result = getattr(service, action)(args.device)
    (args.output / (action + ".json")).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if action == "capabilities":
        print(action, result["options"], flush=True)
    elif action == "generator_status":
        print(action, result, flush=True)
    elif action == "screenshot":
        print(action, {k: result[k] for k in ("image_path", "width", "height", "bytes")}, flush=True)
    else:
        print(action, "PASS", flush=True)
