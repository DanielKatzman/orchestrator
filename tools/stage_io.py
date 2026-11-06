"""Small file adapters run with the appropriate isolated stage interpreter."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.common import atomic, contained, digest, read, rows, sha


class ReviewRequired(ValueError):
    pass


def normalize(source, output, dpi, expected):
    import pymupdf
    from PIL import Image, ImageOps

    source, output = Path(source), Path(output)
    receipt_path = output.parent / "inventory.json"
    if receipt_path.exists():
        receipt = read(receipt_path)
        if (
            receipt["source"]
            != {p.name: sha(p) for p in sorted(source.iterdir()) if p.is_file()}
            or receipt["pdf_dpi"] != dpi
        ):
            raise ValueError("Input inventory changed")
        for item in receipt.get("captures", receipt.get("pieces", [])):
            if sha(contained(output, item["file"])) != item["sha256"]:
                raise ValueError("Normalized input changed")
        return receipt
    if output.exists():
        raise ValueError("Unpublished normalized inputs require an explicit new job")
    output.parent.mkdir(parents=True, exist_ok=True)
    inventory = []
    seen = set()
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        target = Path(tmp) / "captures"
        target.mkdir()

        def save(image, path, page):
            if image.width * image.height > 60_000_000:
                raise ValueError("A capture exceeds 60 million native pixels")
            if len(inventory) >= 60:
                raise ValueError("At most 60 source images/PDF pages per job")
            # Decode/EXIF orientation only; native size is never normalized.
            pixels = image.convert("RGB")
            pixel_id = digest(
                [
                    pixels.size,
                    __import__("hashlib").sha256(pixels.tobytes()).hexdigest(),
                ]
            )
            if pixel_id in seen:
                raise ValueError(
                    "Duplicate image/page content cannot represent distinct physical pieces"
                )
            seen.add(pixel_id)
            name = f"capture_{len(inventory) + 1:04d}.png"
            pixels.save(target / name)
            inventory.append(
                {
                    "file": name,
                    "sha256": sha(target / name),
                    "source_name": path.name,
                    "source_sha256": sha(path),
                    "pdf_page": page,
                    "size": list(pixels.size),
                }
            )

        for path in sorted(source.iterdir()):
            if path.suffix.lower() == ".pdf":
                with pymupdf.open(path) as document:
                    if not document.page_count or document.page_count > 60:
                        raise ValueError("PDF has an invalid page count")
                    for number, page in enumerate(document, 1):
                        if (
                            page.rect.width * page.rect.height * (dpi / 72) ** 2
                            > 60_000_000
                        ):
                            raise ValueError("Rendered PDF page exceeds pixel limit")
                        pix = page.get_pixmap(
                            matrix=pymupdf.Matrix(dpi / 72, dpi / 72),
                            colorspace=pymupdf.csRGB,
                            alpha=False,
                        )
                        save(
                            Image.frombytes(
                                "RGB", (pix.width, pix.height), pix.samples
                            ),
                            path,
                            number,
                        )
            else:
                with Image.open(path) as image:
                    if getattr(image, "n_frames", 1) != 1:
                        raise ValueError(
                            "Use a PDF or separate files for multi-frame images"
                        )
                    save(ImageOps.exif_transpose(image), path, None)
        if not inventory:
            raise ValueError("Provide at least one source image or PDF page")
        os.replace(target, output)
    receipt = {
        "schema_version": "orchestrator.capture-inventory.v2",
        "captures": inventory,
        "expected_physical_pieces": expected,
        "pdf_dpi": dpi,
        "source": {p.name: sha(p) for p in sorted(source.iterdir()) if p.is_file()},
    }
    atomic(receipt_path, receipt)
    return receipt


def gate(root, inventory, expected=0):
    from universal_segmenter.release_cli import verify_output

    result = verify_output(root)
    values = rows(root)
    inputs = read(inventory)
    captures = inputs.get("captures", inputs.get("pieces", []))
    sources = read(Path(root) / "segmentation_run.json").get("sources", [])
    source_ids = {"capture_" + p["sha256"] for p in captures}
    ids = {r["capture_id"] for r in values}
    if not result["complete"] or len(ids) != len(values):
        raise ValueError(
            "Stage 1 publication is incomplete or has duplicate observations"
        )
    if sources:
        if {s["source_capture_id"] for s in sources} != source_ids or len(
            sources
        ) != len(source_ids):
            raise ValueError(
                "Stage 1 source coverage differs from the uploaded captures"
            )
        if any(
            not s.get("review_confirmed")
            or (not s["observation_ids"] and not s.get("empty_reviewed"))
            for s in sources
        ):
            raise ReviewRequired("Confirm every source photo, including empty photos")
    elif ids != source_ids:
        raise ValueError("Stage 1 did not preserve source coverage")
    if any(r.get("empty_proposal") or not r["accepted"] for r in values):
        raise ReviewRequired("One or more pieces require mask review")
    if len(values) > 60 or (expected and len(values) != expected):
        raise ValueError(
            "Reviewed physical-piece count differs from the declared inventory or limit"
        )
    return {
        **result,
        "status": "ready",
        "pieces": len(values),
        "sources": len(captures),
    }


def validate_mask(path, shape):
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    with Image.open(path) as image:
        if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
            raise ValueError("Correction must be a single binary PNG mask")
        value = np.asarray(image.convert("L"))
    if list(value.shape) != list(shape) or not np.isin(value, [0, 255]).all():
        raise ValueError("Mask must be native-size binary PNG (0 or 255)")
    mask = value > 0
    if (
        not mask.any()
        or mask.all()
        or ndimage.label(mask, np.ones((3, 3), bool))[1] != 1
    ):
        raise ValueError("Mask must contain one nonempty connected physical piece")
    if mask[0].any() or mask[-1].any() or mask[:, 0].any() or mask[:, -1].any():
        raise ValueError(
            "Piece touches the capture frame; provide an unclipped capture"
        )
    adapter = Path(__file__).resolve().parents[1] / "composition/adapter"
    sys.path.insert(0, str(adapter))
    from prepare_stage1_for_stage2 import connected_mask

    connected_mask(mask)
    return mask


def publish_review(raw, decisions, output):
    import numpy as np
    from universal_segmenter.inference.preprocess import load_rgb
    from universal_segmenter.pipeline.runner import _materialize
    from universal_segmenter.release_cli import files, verify_output

    raw, decisions, output = Path(raw), Path(decisions), Path(output)
    source_receipt = read(raw / "integration_receipt.json")
    if source_receipt["artifacts"] != {
        p: h for p, h in files(raw).items() if p != "integration_receipt.json"
    }:
        raise ValueError("Raw Stage 1 publication changed")
    if output.exists():
        receipt = read(output / "review_publication.json")
        actual = {
            p: h for p, h in files(output).items() if p != "review_publication.json"
        }
        if actual != receipt["artifacts"] or receipt["decisions_sha256"] != sha(
            decisions
        ):
            raise ValueError("Reviewed publication identity changed")
        return verify_output(output)
    submitted = read(decisions)
    if submitted.get("schema_version") == "orchestrator.instance-review.v2":
        return publish_instances(raw, submitted, decisions, output)
    values = rows(raw)
    ids = {r["capture_id"] for r in values}
    if set(submitted) - ids:
        raise ValueError("Review names an unknown physical piece")
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        target = Path(tmp) / "publication"
        shutil.copytree(raw, target)
        (target / "integration_receipt.json").rename(
            target / "raw_stage1_integration_receipt.json"
        )
        for row in values:
            decision = submitted.get(row["capture_id"])
            if not decision:
                if not row["accepted"]:
                    raise ValueError("Every pending piece needs an explicit approval")
                continue
            base = contained(raw, row["mask_path"])
            if decision["base_sha256"] != sha(base):
                raise ValueError("Review uses a stale original mask")
            mask_path = contained(decisions.parent, decision["mask"])
            if sha(mask_path) != decision["sha256"]:
                raise ValueError("Submitted correction changed")
            mask = validate_mask(mask_path, row["original_size_hw"])
            rgb = load_rgb(Path(row["source_path"]))
            original = (
                np.asarray(
                    __import__("PIL.Image", fromlist=["Image"]).open(base).convert("L")
                )
                > 0
            )
            paths = _materialize(
                target, row.get("artifact_id", row["source_sha256"]), rgb, mask
            )
            yy, xx = np.nonzero(mask)
            row.update(
                bbox_xywh=[
                    int(xx.min()),
                    int(yy.min()),
                    int(xx.max() - xx.min() + 1),
                    int(yy.max() - yy.min() + 1),
                ],
                accepted=True,
                review_status="manually_reviewed",
                decision_source="manual_review",
                reason_codes=[],
                manual_changed_fraction=float(np.mean(mask != original)),
            )
            row.update(
                {
                    key: paths[value]
                    for key, value in [
                        ("mask_path", "mask"),
                        ("piece_path", "piece"),
                        ("piece_mask_path", "piece_mask"),
                        ("overlay_path", "overlay"),
                    ]
                }
            )
        (target / "segmentation_manifest.jsonl").write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in values), "utf-8"
        )
        report = read(target / "segmentation_run.json")
        report.update(
            complete=True, needs_review=0, manually_reviewed_this_run=len(submitted)
        )
        atomic(target / "segmentation_run.json", report)
        profile = read(raw / "capture_profile.json")
        profile["stage1_masks"] = "segformer_v3_reviewed"
        profile["name"] = "shared-native-scale-reviewed-masks.v1"
        atomic(
            target / "capture_profile.json", profile
        )  # First publication, before Stage 2/3 binding.
        result = verify_output(target)
        if result["status"] != "ready":
            raise ValueError("Reviewed batch is not complete")
        atomic(
            target / "review_publication.json",
            {
                "schema_version": "orchestrator.review-publication.v1",
                "policy": "native-mask-review.v1",
                "raw_receipt_sha256": sha(raw / "integration_receipt.json"),
                "decisions_sha256": sha(decisions),
                "publisher_sha256": sha(Path(__file__)),
                "artifacts": files(target),
            },
        )
        os.replace(target, output)
    return result


def publish_instances(raw, submitted, decisions, output):
    import numpy as np
    from universal_segmenter.inference.preprocess import load_rgb
    from universal_segmenter.pipeline.runner import _materialize
    from universal_segmenter.release_cli import files, verify_output

    if submitted["raw_receipt_sha256"] != sha(raw / "integration_receipt.json"):
        raise ValueError("Review refers to another raw publication")
    original = {r["capture_id"]: r for r in rows(raw)}
    templates = {
        r.get("source_capture_id", r["capture_id"]): r for r in original.values()
    }
    if set(submitted["sources"]) != set(templates):
        raise ValueError("Reviewed source inventory changed")
    observations = submitted["observations"]
    if any(not s["confirmed"] for s in submitted["sources"].values()):
        raise ValueError("Every source requires explicit confirmation")
    retained = [
        (pid, item)
        for pid, item in observations.items()
        if not item["empty_proposal"]
        and item["decision"] not in {"rejected", "superseded"}
    ]
    if any(p["decision"] != "approved" for _, p in retained):
        raise ValueError("Every retained piece requires approval")
    if len(retained) > 60 or (
        submitted["expected_pieces"] and len(retained) != submitted["expected_pieces"]
    ):
        raise ValueError("Physical piece count differs from the declared inventory")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as folder:
        target = Path(folder) / "publication"
        shutil.copytree(raw, target)
        (target / "integration_receipt.json").rename(
            target / "raw_stage1_integration_receipt.json"
        )
        values, source_rows = [], {sid: [] for sid in templates}
        cached_source, rgb = None, None
        for pid, item in sorted(
            retained,
            key=lambda pair: (pair[1]["row"].get("source_capture_id", ""), pair[0]),
        ):
            supplied = item["row"]
            sid = supplied.get("source_capture_id", supplied["capture_id"])
            template = templates.get(sid)
            if template is None or supplied["capture_id"] != pid:
                raise ValueError("Piece identity/source differs")
            for field in ("source_sha256", "source_path", "original_size_hw"):
                if supplied[field] != template[field]:
                    raise ValueError("Piece source or native frame changed")
            row = dict(original.get(pid, template))
            if pid in original:
                if item["raw_base_sha256"] != sha(contained(raw, row["mask_path"])):
                    raise ValueError("Correction uses a stale original mask")
                if row.get("empty_proposal"):
                    raise ValueError(
                        "An empty placeholder cannot become an accepted observation"
                    )
            else:
                if (
                    item["origin"] not in {"manual", "split", "merge"}
                    or item["raw_base_sha256"] is not None
                ):
                    raise ValueError("A new piece needs explicit creation provenance")
                if any(
                    parent not in observations
                    or observations[parent]["row"].get("source_capture_id") != sid
                    for parent in item["parents"]
                ):
                    raise ValueError("New piece parents refer to a different source")
                row.update(
                    capture_id=pid,
                    source_capture_id=sid,
                    artifact_id=digest(pid),
                    candidate_id=supplied["candidate_id"],
                )
            row.setdefault("artifact_id", row["source_sha256"])
            mask_path = contained(decisions.parent, item["mask"])
            if sha(mask_path) != item["sha256"]:
                raise ValueError("Reviewed mask changed")
            mask = validate_mask(mask_path, row["original_size_hw"])
            if cached_source != sid:
                image = Path(row["source_path"])
                if sha(image) != row["source_sha256"]:
                    raise ValueError("Native source image changed")
                rgb = load_rgb(image)
                cached_source = sid
            artifact = row.get("artifact_id", row["source_sha256"])
            if not artifact or any(c in artifact for c in ("/", "\\", ":")):
                raise ValueError("Invalid artifact filename key")
            paths = _materialize(target, artifact, rgb, mask)
            yy, xx = np.nonzero(mask)
            row.update(
                bbox_xywh=[
                    int(xx.min()),
                    int(yy.min()),
                    int(xx.max() - xx.min() + 1),
                    int(yy.max() - yy.min() + 1),
                ],
                accepted=True,
                empty_proposal=False,
                review_status="manually_reviewed",
                decision_source="manual_review",
                reason_codes=[],
                review_origin=item["origin"],
                parent_observation_ids=item["parents"],
            )
            row.update(
                {
                    key: paths[value]
                    for key, value in (
                        ("mask_path", "mask"),
                        ("piece_path", "piece"),
                        ("piece_mask_path", "piece_mask"),
                        ("overlay_path", "overlay"),
                    )
                }
            )
            values.append(row)
            source_rows[sid].append(pid)
        if len({r["artifact_id"] for r in values}) != len(values):
            raise ValueError("Piece artifact filename keys collide")
        (target / "segmentation_manifest.jsonl").write_text(
            "".join(json.dumps(r, sort_keys=True) + "\n" for r in values), "utf-8"
        )
        report = read(raw / "segmentation_run.json")
        sources = []
        for sid, source in submitted["sources"].items():
            if not source_rows[sid] and not source["empty_reviewed"]:
                raise ValueError("An empty source needs explicit confirmation")
            sources.append(
                {
                    "source_capture_id": sid,
                    "source_sha256": templates[sid]["source_sha256"],
                    "observation_ids": source_rows[sid],
                    "proposal_count": source.get("proposal_count", 0),
                    "review_confirmed": True,
                    "empty_reviewed": not source_rows[sid],
                    "processed": True,
                }
            )
        report.update(
            capture_mode="multi",
            complete=True,
            source_count=len(sources),
            total_discovered=len(sources),
            sources=sources,
            piece_count=len(values),
            needs_review=0,
            accepted=len(values),
            manually_reviewed_this_run=len(values),
        )
        atomic(target / "segmentation_run.json", report)
        profile = read(raw / "capture_profile.json")
        profile.update(
            stage1_masks="segformer_v3_reviewed",
            name="shared-native-scale-reviewed-masks.v1",
        )
        atomic(target / "capture_profile.json", profile)
        atomic(target / "review_history.json", submitted)
        result = verify_output(
            target
        )  # Includes native mask-overlap and crop/alpha verification.
        if values and result["status"] != "ready":
            raise ValueError("Reviewed piece publication is incomplete")
        atomic(
            target / "review_publication.json",
            {
                "schema_version": "orchestrator.review-publication.v2",
                "policy": "native-instance-review.v2",
                "raw_receipt_sha256": sha(raw / "integration_receipt.json"),
                "decisions_sha256": sha(decisions),
                "publisher_sha256": sha(Path(__file__)),
                "artifacts": files(target),
            },
        )
        os.replace(target, output)
    return {**result, "status": "ready", "pieces": len(values)}


def split_mask(path, shape, output):
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    with Image.open(path) as image:
        value = np.asarray(image.convert("L"))
    if list(value.shape) != list(shape) or not np.isin(value, [0, 255]).all():
        raise ValueError("Split requires a native binary mask")
    labels, count = ndimage.label(value > 0, np.ones((3, 3), bool))
    if not 2 <= count <= 60:
        raise ValueError(
            "Erase the bridge and save a draft containing 2–60 separate regions before splitting"
        )
    output.mkdir(parents=True)
    result = []
    for number in range(1, count + 1):
        name = f"part_{number:04d}.png"
        Image.fromarray((labels == number).astype(np.uint8) * 255).save(output / name)
        validate_mask(output / name, shape)
        result.append(name)
    return {"masks": result}


def boundary(source, output):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "composition/adapter"))
    from prepare_stage1_for_stage2 import prepare

    if output.exists():
        receipt = read(output / "mask_boundary_adapter.json")
        if receipt["source_manifest_sha256"] != sha(
            source / "segmentation_manifest.jsonl"
        ):
            raise ValueError("Boundary input identity changed")
        # Completed derived trees are authenticated by an application seal.
        seal = read(output / "orchestrator_boundary_seal.json")
        for name, expected in seal["files"].items():
            if sha(contained(output, name)) != expected:
                raise ValueError("Boundary publication changed")
        return receipt
    try:
        with redirect_stdout(sys.stderr):
            receipt = prepare(source, output)
    except ValueError as error:
        if str(error).startswith("Mask requires review:"):
            raise ReviewRequired(str(error)) from error
        raise
    atomic(
        output / "orchestrator_boundary_seal.json",
        {
            "files": {
                p.relative_to(output).as_posix(): sha(p)
                for p in output.rglob("*")
                if p.is_file()
            }
        },
    )
    return receipt


def verify4(
    root,
    bundle,
    capture,
    config_path,
    profile_path,
    inventory,
    stage1=None,
    expected_count=0,
):
    from paper_reconstruction_contracts import load_bundle
    from paper_reconstruction_stage4.assessment import verify_package
    from paper_reconstruction_stage4.config import SolverConfig
    from paper_reconstruction_stage4.outputs import digest as native_digest
    from paper_reconstruction_stage4.outputs import (
        file_hash,
        implementation_identity,
        read_result,
    )

    evidence = load_bundle(bundle, full=True)
    result = read_result(root)
    assessment_root = root.with_name(root.name + ".assessment")
    manifest = verify_package(assessment_root)
    assessment = read(assessment_root / "assessment.json")
    binding = manifest["binding"]
    config = SolverConfig.from_dict(read(config_path))
    if (
        binding != assessment["binding"]
        or binding["result_sha256"] != file_hash(root / "result.json")
        or binding["input_manifest_sha256"] != evidence.manifest["manifest_sha256"]
        or binding["source_run_id"] != result["run_id"]
        or binding["configuration_sha256"] != native_digest(config.to_dict())
        or binding["assessment_implementation_sha256"]
        != implementation_identity()["sha256"]
        or binding["source_implementation_sha256"] != result["implementation"]["sha256"]
        or binding["constraint_profile"]["capture_profile_sha256"] != sha(capture)
        or binding["constraint_profile"]["profile_file_sha256"] != sha(profile_path)
        or result["input"]["manifest_sha256"] != evidence.manifest["manifest_sha256"]
    ):
        raise ValueError("Result/assessment/evidence/profile binding differs")
    if stage1:
        accepted = rows(stage1)
        if any(not p["accepted"] or p.get("empty_proposal") for p in accepted):
            raise ValueError(
                "Only accepted physical observations may enter reconstruction"
            )
        expected = {p["capture_id"] for p in accepted}
        if len(expected) != len(accepted) or (
            expected_count and len(expected) != expected_count
        ):
            raise ValueError("Accepted physical inventory differs")
    else:
        expected = {"capture_" + p["sha256"] for p in read(inventory)["pieces"]}
    if set(evidence.manifest["pieces"]) != expected:
        raise ValueError("Stage 3 evidence differs from accepted physical inventory")
    layout = result["layouts"][0]
    if {p["piece_id"] for p in layout["pieces"]} != expected or len(
        layout["pieces"]
    ) != len(expected):
        raise ValueError("Result lost physical pieces")
    primary = assessment["layouts"][0]
    supported = (
        primary["status"] == "supported"
        and primary["native_support_valid"]
        and primary["native_final_validation"]["valid"]
        and layout["all_pieces_connected"]
    )
    outcome = (
        "supported"
        if supported
        else (
            "unsupported_input"
            if primary["status"] == "profile_violation"
            else "partial"
            if not layout["all_pieces_connected"]
            else "unresolved"
        )
    )
    return {
        "outcome": outcome,
        "pieces": len(expected),
        "connected": layout["all_pieces_connected"],
        "joins": len(layout["selected_joins"]),
        "assessment": primary["status"],
        "reason_codes": primary["reason_codes"],
        "termination": result["termination"],
        "run_id": result["run_id"],
        "result_sha256": sha(root / "result.json"),
        "assessment_manifest_sha256": sha(assessment_root / "manifest.json"),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "command",
        choices=["normalize", "gate", "boundary", "review", "mask", "split", "verify4"],
    )
    for name in [
        "input",
        "output",
        "inventory",
        "decisions",
        "bundle",
        "capture",
        "config",
        "profile",
        "stage1",
    ]:
        parser.add_argument("--" + name, type=Path)
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--expected", type=int, default=0)
    parser.add_argument("--shape", nargs=2, type=int)
    args = parser.parse_args()
    try:
        if args.command == "normalize":
            result = normalize(args.input, args.output, args.dpi, args.expected)
        elif args.command == "gate":
            result = gate(args.input, args.inventory, args.expected)
        elif args.command == "boundary":
            result = boundary(args.input, args.output)
        elif args.command == "review":
            result = publish_review(args.input, args.decisions, args.output)
        elif args.command == "mask":
            validate_mask(args.input, args.shape)
            result = {"valid": True}
        elif args.command == "split":
            result = split_mask(args.input, args.shape, args.output)
        else:
            result = verify4(
                args.input,
                args.bundle,
                args.capture,
                args.config,
                args.profile,
                args.inventory,
                args.stage1,
                args.expected,
            )
        print(json.dumps(result, allow_nan=False))
        return 0
    except ReviewRequired as error:
        print(json.dumps({"review_required": str(error)}))
        return 3
    except Exception as error:
        print(
            json.dumps({"error": f"{type(error).__name__}: {error}"}), file=sys.stderr
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
