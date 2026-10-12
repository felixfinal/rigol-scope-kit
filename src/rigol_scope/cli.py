from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .config import Settings
from .errors import ScopeError
from .service import ScopeService
from .catalog import list_captures, capture_info
from .reports import derive, derive_timing
from .contracts import get_contract
from .profiles import list_profiles, get_profile


def main() -> int:
    parser = argparse.ArgumentParser(description="MHO900 bounded LAN SCPI")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--device", default="bench-scope")
    controls = {"configure-channel": "configure_channel", "configure-timebase": "configure_timebase", "configure-trigger": "configure_trigger", "configure-pulse-trigger": "configure_pulse_trigger", "configure-acquisition": "configure_acquisition", "control": "acquisition_control", "generator-configure": "configure_generator", "generator-output": "generator_output", "generator-sync-phase": "synchronize_phase", "wake":"wake", "release":"release", "configure-trigger-options":"configure_trigger_options"}
    parser.add_argument("--params", type=Path, help="JSON object with named tool arguments, including confirmed for changes")
    parser.add_argument("action", choices=["prepare-capture","prepared-status","prepared-collect","prepared-cancel","prepared-generator-output","trigger-options","power-status", "identify", "capabilities", "status", "screenshot", "generator-status", "doctor", "measure", "measure-timing", "waveform-export", "capture-single", "captures-list", "capture-info", "waveform-plot", "waveform-analyze", "waveform-search", "waveform-timing", "contract", "profiles-list", "profile-get", "measurement-preview", "measurement-capture", *controls])
    args = parser.parse_args()
    try:
        if args.action == "contract":
            print(json.dumps(get_contract(), ensure_ascii=False, indent=2))
            return 0
        config = Settings.load(args.config)
        service = ScopeService(config)
        params = json.loads(args.params.read_text()) if args.params else {}
        if not isinstance(params, dict): raise ScopeError("--params must contain a JSON object")
        if args.action=="profiles-list":
            result=list_profiles(config.profiles_dir)
        elif args.action=="profile-get":
            result={"profile":get_profile(config.profiles_dir,**params),"instrument_access":False}
        elif args.action in {"measurement-preview","measurement-capture"}:
            result=getattr(service,args.action.replace("-","_"))(args.device,**params)
        elif args.action=="captures-list":
            result=list_captures(config.state_dir,**params)
        elif args.action=="capture-info":
            result=capture_info(config.state_dir,**params)
        elif args.action=="waveform-timing":
            result=derive_timing(config.state_dir,**params)
        elif args.action in {"waveform-plot","waveform-analyze","waveform-search"}:
            result=derive(config.state_dir,operation=args.action.split("-")[1],**params)
        elif args.action in {"prepare-capture","prepared-status","prepared-collect","prepared-cancel","prepared-generator-output"}:
            result=getattr(service,args.action.replace("-","_"))(args.device,**params)
        elif args.action=="wake":
            result=service.wake(args.device,**params)
        elif args.action in controls:
            result = service.execute_control(args.device, controls[args.action], **params)
        else:
            result = getattr(service, args.action.replace("-", "_"))(args.device, **params)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("verified") is False else 0
    except (ScopeError, OSError, TypeError, ValueError) as exc:
        print(json.dumps({"error": str(exc), "error_type": type(exc).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
