"""Generate a current, offline request template; inspect wiring before using it."""
import json

from rigol_scope.contracts import CONTRACT_VERSION, MeasurementRequest
from rigol_scope.profiles import get_profile

profile = get_profile(None, "passive-10x")
request = MeasurementRequest(
    contract_version=CONTRACT_VERSION,
    profile_id=profile["profile_id"],
    profile_version=profile["version"],
    profile_digest=profile["digest"],
    channels=[1],
    expected_signal={"objective": "Replace with the actual task and wiring"},
)
print(json.dumps({"request": request.model_dump(mode="json", exclude_none=True), "confirmed": False}, indent=2))
