"""Durable, per-source instance review; raw model publications stay immutable."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import threading
import uuid
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageChops

from .common import atomic, contained, digest, now, read, rows, sha
from .config import ROOT

_locks = defaultdict(threading.RLock)


def review_lock(job_id):
    return _locks[job_id]


def raw_root(settings, job):
    attempt = settings.attempt(job)
    meta = read(attempt / "attempt.json") if (attempt / "attempt.json").exists() else {}
    return (
        settings.job(job["id"])
        / "attempts"
        / f"{meta.get('raw_attempt', job['attempt']):04d}"
        / "stage1_native"
    )


class Review:
    def __init__(self, settings, job):
        self.settings, self.job = settings, job
        self.raw = raw_root(settings, job)
        attempt = settings.attempt(job)
        metadata = (
            read(attempt / "attempt.json")
            if (attempt / "attempt.json").exists()
            else {}
        )
        if metadata.get("kind") == "assembly":
            attempt = attempt.parent / f"{metadata['evidence_attempt']:04d}"
            metadata = read(attempt / "attempt.json")
        self.folder = attempt / "review"
        self.path = self.folder / "state.json"
        self.state = None
        if not (self.raw / "integration_receipt.json").is_file():
            return
        receipt = read(self.raw / "integration_receipt.json")
        if receipt["artifacts"].get("segmentation_manifest.jsonl") != sha(
            self.raw / "segmentation_manifest.jsonl"
        ):
            raise ValueError("Raw proposal manifest changed")
        frozen = self.folder / "decisions.json"
        legacy = {}
        if self.path.exists() or frozen.exists():
            self.state = read(self.path if self.path.exists() else frozen)
            if self.state.get("schema_version") != "orchestrator.instance-review.v2":
                legacy, self.state = self.state, None
        if self.state is not None:
            if self.state["raw_receipt_sha256"] != sha(
                self.raw / "integration_receipt.json"
            ):
                raise ValueError("Raw Stage 1 receipt changed")
            if not self.path.exists():
                if (
                    metadata.get("review_sha256")
                    and sha(frozen) != metadata["review_sha256"]
                ):
                    raise ValueError("Frozen review snapshot changed")
                for piece in self.state["observations"].values():
                    if (
                        piece["decision"] in {"approved", "pending"}
                        and not piece["empty_proposal"]
                    ):
                        piece["mask"] = (
                            (self.folder / piece["mask"])
                            .relative_to(settings.job(job["id"]))
                            .as_posix()
                        )
            return
        # Initialize only from a complete, sealed Stage 1 publication.
        run = read(self.raw / "segmentation_run.json")
        if not run["complete"]:
            return
        values = rows(self.raw)
        sources = copy.deepcopy(run.get("sources", []))
        if not sources:  # Read-only display of historical single-piece jobs.
            sources = [
                {
                    "source_capture_id": r["capture_id"],
                    "source_sha256": r["source_sha256"],
                    "observation_ids": [r["capture_id"]],
                    "empty_reviewed": False,
                    "proposal_count": 1,
                }
                for r in values
            ]
        self.state = {
            "schema_version": "orchestrator.instance-review.v2",
            "raw_receipt_sha256": sha(self.raw / "integration_receipt.json"),
            "sources": {},
            "observations": {},
            "source_order": [source["source_capture_id"] for source in sources],
            "updated": now(),
        }
        by_source = {}
        for order, row in enumerate(values):
            source = row.get("source_capture_id", row["capture_id"])
            image = Path(row["source_path"])
            if not image.resolve().is_relative_to(settings.job(job["id"]).resolve()):
                raise ValueError("Source image escapes this job")
            mask = contained(self.raw, row["mask_path"])
            checksum = sha(mask)
            if receipt["artifacts"].get(row["mask_path"]) != checksum:
                raise ValueError("Raw proposal mask changed")
            by_source[source] = row
            self.state["observations"][row["capture_id"]] = {
                "row": row,
                "mask": mask.relative_to(settings.job(job["id"])).as_posix(),
                "sha256": checksum,
                "raw_base_sha256": checksum,
                "decision": "approved" if row["accepted"] else "pending",
                "origin": "model",
                "parents": [],
                "empty_proposal": bool(row.get("empty_proposal")),
                "at": now(),
                "order": order,
            }
        for source in sources:
            template = by_source.get(source["source_capture_id"])
            if template is None:
                raise ValueError(
                    "Raw source has no proposal or explicit empty placeholder"
                )
            self.state["sources"][source["source_capture_id"]] = {
                **source,
                "row_template": template,
                "confirmed": False,
                "empty_reviewed": False,
            }
        for pid, piece in self.state["observations"].items():
            draft = self.folder / "drafts" / (pid + ".json")
            correction = legacy.get(pid) or (read(draft) if draft.exists() else None)
            if correction:
                mask = contained(self.folder, correction["mask"])
                if sha(mask) != correction["sha256"]:
                    raise ValueError("Historical correction changed")
                piece.update(
                    mask=mask.relative_to(settings.job(job["id"])).as_posix(),
                    sha256=correction["sha256"],
                    decision="approved",
                )
        if (
            job["state"] == "review_required"
            and job["request"].get("application_sha256") == settings.application_hash
        ):
            self.save("opened", {})

    @property
    def revision(self):
        return digest(self.state) if self.state else ""

    def check_revision(self, revision):
        if revision is not None and revision != self.revision:
            raise ValueError("Review changed in another window; reload before editing")

    def save(self, action, detail):
        self.state["updated"] = now()
        atomic(
            self.folder / "history" / (uuid.uuid4().hex + ".json"),
            {"action": action, "detail": detail, "state": self.state},
        )
        atomic(self.path, self.state)

    def source(self, source_id):
        if not self.state or source_id not in self.state["sources"]:
            raise ValueError("Unknown source photo")
        return self.state["sources"][source_id]

    def piece(self, piece_id):
        if not self.state or piece_id not in self.state["observations"]:
            raise ValueError("Unknown piece")
        return self.state["observations"][piece_id]

    def source_id(self, piece):
        return piece["row"].get("source_capture_id", piece["row"]["capture_id"])

    def mask_path(self, piece):
        path = contained(self.settings.job(self.job["id"]), piece["mask"])
        if sha(path) != piece["sha256"]:
            raise ValueError("Review mask changed on disk")
        return path

    def active(self, source_id=None):
        return [
            (pid, p)
            for pid, p in self.state["observations"].items()
            if p["decision"] not in {"rejected", "superseded"}
            and not p["empty_proposal"]
            and (source_id is None or self.source_id(p) == source_id)
        ]

    def invalidate(self, source_id):
        self.source(source_id).update(confirmed=False, empty_reviewed=False)

    def binary(self, path, shape):
        with Image.open(path) as image:
            if image.format != "PNG" or getattr(image, "n_frames", 1) != 1:
                raise ValueError("Use a single native-size binary PNG")
            if image.size != (shape[1], shape[0]):
                raise ValueError("Mask must match the source photo's native dimensions")
            value = image.convert("L")
            colors = value.getcolors(256)
            if (
                value.size != (shape[1], shape[0])
                or colors is None
                or any(color not in (0, 255) for _, color in colors)
            ):
                raise ValueError("Mask must be native-size binary PNG (0 or 255)")
            return value.copy()

    def native(self, command, path, shape=None, output=None):
        args = [
            self.settings.python("stage1"),
            str(ROOT / "tools/stage_io.py"),
            command,
            "--input",
            str(path),
        ]
        if shape:
            args += ["--shape", *map(str, shape)]
        if output:
            args += ["--output", str(output)]
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            env=self.settings.environment("stage1"),
            timeout=60,
        )
        if result.returncode:
            message = (result.stderr or result.stdout)[-1400:]
            try:
                message = json.loads(result.stderr or result.stdout).get(
                    "error", message
                )
            except (ValueError, TypeError):
                pass
            raise ValueError(message)
        return json.loads(result.stdout)

    def store_mask(self, mask):
        path = self.folder / "masks" / (uuid.uuid4().hex + ".png")
        path.parent.mkdir(parents=True, exist_ok=True)
        mask.save(path)
        return path, sha(path)

    def no_overlap(self, source_id, mask, ignore=()):
        for pid, other in self.active(source_id):
            if pid in ignore:
                continue
            with Image.open(self.mask_path(other)) as previous:
                if ImageChops.multiply(mask, previous.convert("L")).getbbox():
                    raise ValueError(
                        "Mask overlaps another retained piece in this photo; correct or reject that proposal first"
                    )

    def edit(self, piece_id, base_sha256, payload, decision="approved"):
        piece = self.piece(piece_id)
        if piece["decision"] in {"rejected", "superseded"} or piece["empty_proposal"]:
            raise ValueError(
                "Select an active piece, or add a missed piece to an empty photo"
            )
        if piece["sha256"] != base_sha256:
            raise ValueError("Mask revision changed; reload it")
        original = self.mask_path(piece)
        path = self.folder / "masks" / (uuid.uuid4().hex + ".png")
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_bytes(payload) if payload is not None else shutil.copyfile(
                original, path
            )
            mask = self.binary(path, piece["row"]["original_size_hw"])
            if decision not in {"pending", "approved"}:
                raise ValueError("Unknown review decision")
            if decision == "approved":
                self.native("mask", path, piece["row"]["original_size_hw"])
            self.no_overlap(self.source_id(piece), mask, [piece_id])
            box = mask.getbbox()
            piece["row"]["bbox_xywh"] = (
                [box[0], box[1], box[2] - box[0], box[3] - box[1]]
                if box
                else [0, 0, mask.width, mask.height]
            )
            piece.update(
                mask=path.relative_to(self.settings.job(self.job["id"])).as_posix(),
                sha256=sha(path),
                decision=decision,
                at=now(),
            )
            self.invalidate(self.source_id(piece))
            self.save(
                "mask_" + decision,
                {"piece_id": piece_id, "mask_sha256": piece["sha256"]},
            )
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def add(self, source_id, mask=None, origin="manual", parents=()):
        source = self.source(source_id)
        if len(self.active()) >= 60:
            raise ValueError(
                "At most 60 retained physical pieces; reject unwanted proposals first"
            )
        row = copy.deepcopy(source["row_template"])
        ident = source_id + "_manual_" + uuid.uuid4().hex
        row.update(
            capture_id=ident,
            source_capture_id=source_id,
            candidate_id="manual_" + uuid.uuid4().hex,
            artifact_id=digest(ident),
            accepted=False,
            empty_proposal=False,
            review_status="needs_review",
            decision_source="manual_review",
            reason_codes=["MANUAL_REVIEW_REQUIRED"],
        )
        if mask is None:
            h, w = row["original_size_hw"]
            mask = Image.new("L", (w, h))
        box = mask.getbbox()
        row["bbox_xywh"] = (
            [box[0], box[1], box[2] - box[0], box[3] - box[1]]
            if box
            else [0, 0, mask.width, mask.height]
        )
        path, checksum = self.store_mask(mask)
        self.state["observations"][ident] = {
            "row": row,
            "mask": path.relative_to(self.settings.job(self.job["id"])).as_posix(),
            "sha256": checksum,
            "raw_base_sha256": None,
            "decision": "pending",
            "origin": origin,
            "parents": list(parents),
            "empty_proposal": False,
            "at": now(),
            "order": max(
                (p.get("order", 0) for p in self.state["observations"].values()),
                default=-1,
            )
            + 1,
        }
        self.invalidate(source_id)
        return ident

    def decide(self, piece_id, decision):
        piece = self.piece(piece_id)
        if (
            piece["empty_proposal"]
            or piece["decision"] == "superseded"
            or decision not in {"rejected", "pending"}
        ):
            raise ValueError("This proposal cannot be rejected or restored")
        piece.update(decision=decision, at=now())
        self.invalidate(self.source_id(piece))
        self.save(decision, {"piece_id": piece_id})

    def split(self, piece_id):
        piece = self.piece(piece_id)
        if piece["decision"] not in {"pending", "approved"} or piece["empty_proposal"]:
            raise ValueError("Select an active piece to split")
        output = self.folder / "splits" / uuid.uuid4().hex
        result = self.native(
            "split", self.mask_path(piece), piece["row"]["original_size_hw"], output
        )
        if len(self.active()) - 1 + len(result["masks"]) > 60:
            raise ValueError("Split exceeds the physical-piece limit")
        piece["decision"] = "superseded"
        ids = []
        for name in result["masks"]:
            with Image.open(contained(output, name)) as mask:
                ids.append(
                    self.add(
                        self.source_id(piece), mask.convert("L"), "split", [piece_id]
                    )
                )
        self.save("split", {"parent": piece_id, "children": ids})
        return ids

    def merge(self, piece_ids):
        if len(set(piece_ids)) != len(piece_ids) or len(piece_ids) < 2:
            raise ValueError("Select at least two distinct pieces from one photo")
        pieces = [self.piece(pid) for pid in piece_ids]
        sources = {self.source_id(p) for p in pieces}
        if len(sources) != 1 or any(
            p["empty_proposal"] or p["decision"] not in {"pending", "approved"}
            for p in pieces
        ):
            raise ValueError("Only active pieces from the same photo can merge")
        mask = Image.new("L", tuple(reversed(pieces[0]["row"]["original_size_hw"])))
        for piece in pieces:
            with Image.open(self.mask_path(piece)) as value:
                mask = ImageChops.lighter(mask, value.convert("L"))
        for piece in pieces:
            piece["decision"] = "superseded"
        ident = self.add(next(iter(sources)), mask, "merge", piece_ids)
        self.save("merge", {"parents": piece_ids, "child": ident})
        return ident

    def confirm(self, source_id, empty):
        source = self.source(source_id)
        pieces = self.active(source_id)
        if any(p["decision"] != "approved" for _, p in pieces):
            raise ValueError("Approve or reject every piece in this photo first")
        if empty != (not pieces):
            raise ValueError(
                "An empty photo needs explicit empty confirmation; retain no pieces before confirming it empty"
            )
        source.update(confirmed=True, empty_reviewed=empty)
        self.save("source_confirmed", {"source_capture_id": source_id, "empty": empty})

    def freeze(self, destination, expected, reconstruct, acquisition_confirmed):
        if not self.state or any(
            not s["confirmed"] for s in self.state["sources"].values()
        ):
            raise ValueError(
                "Confirm every source photo, including photos with no pieces"
            )
        pieces = self.active()
        if any(p["decision"] != "approved" for _, p in pieces):
            raise ValueError("Approve every retained piece first")
        if expected and len(pieces) != expected:
            raise ValueError(
                f"Expected {expected} physical pieces, but review retains {len(pieces)}"
            )
        if reconstruct and (not pieces or not acquisition_confirmed):
            raise ValueError(
                "Reconstruction requires pieces from one complete page at a common native scale"
            )
        if destination.exists():
            raise ValueError("An unpublished review attempt already exists")
        snapshot = copy.deepcopy(self.state)
        snapshot.update(
            expected_pieces=expected,
            reconstruct=reconstruct,
            acquisition_confirmed=acquisition_confirmed,
        )
        try:
            for pid, piece in snapshot["observations"].items():
                if (
                    piece["decision"] not in {"approved", "pending"}
                    or piece["empty_proposal"]
                ):
                    continue
                source = self.mask_path(self.piece(pid))
                target = destination / "review/masks" / (piece["sha256"] + ".png")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                piece["mask"] = target.relative_to(destination / "review").as_posix()
            atomic(destination / "review/decisions.json", snapshot)
            return snapshot
        except BaseException:
            if destination.exists():
                shutil.rmtree(destination)
            raise
