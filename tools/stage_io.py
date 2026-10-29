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
        for item in receipt["pieces"]:
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
                raise ValueError("At most 60 pieces per job")
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
            name = f"piece_{len(inventory) + 1:04d}.png"
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
        if not inventory or (expected and len(inventory) != expected):
            raise ValueError(
                "Expanded capture count differs from the expected physical inventory"
            )
        os.replace(target, output)
    receipt = {
        "pieces": inventory,
        "pdf_dpi": dpi,
        "source": {p.name: sha(p) for p in sorted(source.iterdir()) if p.is_file()},
    }
    atomic(receipt_path, receipt)
    return receipt


def gate(root, inventory):
    from universal_segmenter.release_cli import verify_output

    result = verify_output(root)
    values = rows(root)
    expected = {"capture_" + p["sha256"] for p in read(inventory)["pieces"]}
    if (
        {r["capture_id"] for r in values} != expected
        or len(values) != len(expected)
        or not result["complete"]
    ):
        raise ValueError("Stage 1 did not preserve the complete physical inventory")
    if not values or not all(r["accepted"] for r in values):
        raise ReviewRequired("One or more pieces require mask review")
    return result


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
    source_receipt = read(raw / "integration_receipt.json")
    if source_receipt["artifacts"] != {
        p: h for p, h in files(raw).items() if p != "integration_receipt.json"
    }:
        raise ValueError("Raw Stage 1 publication changed")
    submitted = read(decisions)
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
            paths = _materialize(target, row["source_sha256"], rgb, mask)
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


def verify4(root, bundle, capture, config_path, profile_path, inventory):
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
    expected = {"capture_" + p["sha256"] for p in read(inventory)["pieces"]}
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
        choices=["normalize", "gate", "boundary", "review", "mask", "verify4"],
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
            result = gate(args.input, args.inventory)
        elif args.command == "boundary":
            result = boundary(args.input, args.output)
        elif args.command == "review":
            result = publish_review(args.input, args.decisions, args.output)
        elif args.command == "mask":
            validate_mask(args.input, args.shape)
            result = {"valid": True}
        else:
            result = verify4(
                args.input,
                args.bundle,
                args.capture,
                args.config,
                args.profile,
                args.inventory,
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
