import json
from pathlib import Path

import pytest

from tools.resolve import compatibility


def metadata():
    audit = json.loads(
        (Path(__file__).parents[1] / "research/SOURCE_AUDIT.json").read_text("utf-8")
    )
    names = [
        "paper-segmentation-stage",
        "paper-reconstruction-stage2",
        "paper-reconstruction-stage3",
        "paper-reconstruction-stage4",
    ]
    return {
        f"stage{i + 1}": audit["repositories"][name]["compatibility"]
        for i, name in enumerate(names)
    }


def test_known_v2_packaging_tuple_is_compatible():
    compatibility(metadata())


def multi_metadata():
    value = metadata()
    value["stage1"]["release_version"] = "2.5.0"
    value["stage1"]["orchestrator_migration_required"] = True
    value["stage1"]["interface"].update(
        capture_mode_default="multi", source_inventory="segmentation_run.json:sources"
    )
    return value


def test_known_multi_piece_migration_is_compatible():
    compatibility(multi_metadata())


@pytest.mark.parametrize(
    "field,value",
    [("capture_mode_default", "unknown"), ("source_inventory", "unknown")],
)
def test_unknown_source_contract_cannot_be_bypassed(field, value):
    data = multi_metadata()
    data["stage1"]["interface"][field] = value
    with pytest.raises(ValueError, match="Unsupported|unsupported"):
        compatibility(data, candidate=True)


def test_model_mismatch_cannot_be_bypassed_by_candidate_flag():
    value = metadata()
    value["stage2"]["model"]["sha256"] = "wrong"
    with pytest.raises(ValueError, match="authenticate"):
        compatibility(value, candidate=True)


def test_new_solver_requires_explicit_candidate_qualification():
    value = metadata()
    value["stage4"]["stages"]["stage4"]["source_sdk_fingerprint"] = "new-solver"
    with pytest.raises(ValueError, match="qualified Stage 4"):
        compatibility(value)
    compatibility(value, candidate=True)
