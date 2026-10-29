"""Reproduce the source-only checks supporting the orchestration plan."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent
SOURCES = ROOT / "sources"
REPOSITORIES = (
    "paper-segmentation-stage",
    "paper-reconstruction-stage2",
    "paper-reconstruction-stage3",
    "paper-reconstruction-stage4",
)


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


audit = {
    "checked_at_utc": datetime.now(timezone.utc).isoformat(),
    "scope": "Source and included Stage 3 assets; no inference or Docker qualification",
    "repositories": {},
}
for name in REPOSITORIES:
    source = SOURCES / name
    entry = {
        "commit": subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
        ).strip(),
        "compatibility": json.loads((source / "compatibility.json").read_text("utf-8")),
        "readmes": {
            p.relative_to(source).as_posix(): checksum(p)
            for p in sorted(source.rglob("*"))
            if p.is_file() and p.name.upper().startswith("README") and ".git" not in p.parts
        },
    }
    inventory_path = source / "provenance/runtime_sources.json"
    if not inventory_path.exists():
        inventory_path = source / "provenance/qualified_sources.json"
    if inventory_path.exists():
        inventory = json.loads(inventory_path.read_text("utf-8"))["files"]
        passed, missing_assets, mismatches = [], [], []
        for relative, expected in inventory.items():
            path = source / relative
            if path.exists() and path.read_bytes().startswith(b"version https://git-lfs.github.com/spec/v1"):
                missing_assets.append(relative)
            elif not path.is_file() or checksum(path) != expected:
                mismatches.append(relative)
            else:
                passed.append(relative)
        entry["inventory_check"] = {
            "matched_files": len(passed),
            "unhydrated_lfs_assets": missing_assets,
            "mismatches": mismatches,
        }
        if mismatches:
            raise SystemExit(f"Source inventory mismatch in {name}: {mismatches}")
    elif name == "paper-reconstruction-stage4":
        component_path = source / "provenance/components.json"
        if checksum(component_path) != entry["compatibility"]["component_inventory_sha256"]:
            raise SystemExit("Stage 4 component inventory digest differs")
        components = json.loads(component_path.read_text("utf-8"))
        counts = {}
        for component, record in components.items():
            component_source = source / "stages" / component / "src"
            actual = {
                p.relative_to(component_source).as_posix(): checksum(p)
                for p in component_source.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
                and not any(part.endswith(".egg-info") for part in p.parts)
            }
            if actual != record["files"]:
                raise SystemExit(f"Stage 4 archived component source differs: {component}")
            counts[component] = len(actual)
        entry["inventory_check"] = {
            "component_source_files": counts,
            "mismatches": [],
            "model_payloads_verified": False,
        }
    audit["repositories"][name] = entry

sdk3 = SOURCES / REPOSITORIES[2] / "contracts/stage3_to_stage4/v2/sdk/src/paper_reconstruction_contracts"
sdk4 = SOURCES / REPOSITORIES[3] / "contracts/sdk/src/paper_reconstruction_contracts"
sdk_files = {
    p.relative_to(sdk3).as_posix(): checksum(p)
    for p in sdk3.rglob("*")
    if p.is_file() and p.suffix in {".py", ".json"} and "__pycache__" not in p.parts
}
sdk4_files = {
    p.relative_to(sdk4).as_posix(): checksum(p)
    for p in sdk4.rglob("*")
    if p.is_file() and p.suffix in {".py", ".json"} and "__pycache__" not in p.parts
}
if sdk_files != sdk4_files:
    raise SystemExit("Stage 3/4 SDK source files differ")
audit["sdk"] = {"byte_identical": True, "files": dict(sorted(sdk_files.items()))}
(ROOT / "SOURCE_AUDIT.json").write_text(
    json.dumps(audit, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
)
print(json.dumps({
    "commits": {n: e["commit"] for n, e in audit["repositories"].items()},
    "inventory_checks": {n: e.get("inventory_check") for n, e in audit["repositories"].items()},
    "sdk_files_identical": len(sdk_files),
    "readmes_inventoried": sum(len(e["readmes"]) for e in audit["repositories"].values()),
}, indent=2))
