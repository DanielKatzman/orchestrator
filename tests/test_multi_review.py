"""Real native publication/geometry checks around an independent source inventory."""

import io
import json
import sys

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from universal_segmenter.pipeline.runner import _materialize
from universal_segmenter.release_cli import files, verify_output

from app.api import create_app
from app.common import atomic, read, rows, sha
from app.config import ROOT, Settings
from app.worker import Coordinator
from tools.stage_io import gate, normalize, publish_review


def png(image):
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def scene(tmp_path):
    settings = Settings(
        tmp_path,
        ROOT / "composition/pipeline.lock.json",
        tmp_path / "components",
        {name: sys.executable for name in ("stage1", "stage2", "stage3", "stage4")},
    )
    app = create_app(settings)
    client = TestClient(app)

    def make(counts=(2, 0), masks=None, expected=0):
        captures = [
            Image.new("RGB", (100, 90), (40 + i * 40, 75, 85))
            for i in range(len(counts))
        ]
        response = client.post(
            "/api/v1/jobs",
            data={"name": "Multi source", "expected_pieces": str(expected)},
            files=[
                ("files", (f"photo{i}.png", png(image)))
                for i, image in enumerate(captures)
            ],
        )
        assert response.status_code == 202, response.text
        job = response.json()
        root = settings.job(job["id"])
        normalized = root / "inputs/normalized"
        normalized.mkdir()
        raw = root / "attempts/0001/stage1_native"
        raw.mkdir(parents=True)
        values, sources, inventory = [], [], []
        for source_number, (image, count) in enumerate(zip(captures, counts), 1):
            source = normalized / f"capture_{source_number:04d}.png"
            image.save(source)
            checksum = sha(source)
            sid = "capture_" + checksum
            observation_ids = []
            for i in range(max(1, count)):
                if masks is not None and source_number == 1:
                    mask = masks[i]
                else:
                    mask = Image.new("L", image.size)
                    if count:
                        x = 6 + i * 12
                        ImageDraw.Draw(mask).rectangle((x, 12, x + 7, 70), fill=255)
                # Deliberately opaque per-piece IDs, unrelated to file hashes.
                ident = (
                    f"observation-{source_number}-{i}"
                    if count
                    else f"empty-{source_number}"
                )
                artifact = f"artifact-{source_number}-{i}"
                materialized = _materialize(
                    raw, artifact, np.asarray(image), np.asarray(mask) > 0
                )
                box = mask.getbbox() or (0, 0, 1, 1)
                values.append(
                    {
                        "schema_version": "paper-segmentation-manifest-1.0",
                        "capture_id": ident,
                        "source_capture_id": sid,
                        "source_sha256": checksum,
                        "source_path": str(source),
                        "original_size_hw": [image.height, image.width],
                        "candidate_id": f"candidate-{i}",
                        "artifact_id": artifact,
                        "bbox_xywh": [box[0], box[1], box[2] - box[0], box[3] - box[1]],
                        "accepted": False,
                        "review_status": "needs_review",
                        "reason_codes": ["MANUAL_REVIEW_REQUIRED"],
                        "empty_proposal": not count,
                        **{
                            key: materialized[value]
                            for key, value in (
                                ("mask_path", "mask"),
                                ("piece_path", "piece"),
                                ("piece_mask_path", "piece_mask"),
                                ("overlay_path", "overlay"),
                            )
                        },
                    }
                )
                observation_ids.append(ident)
            sources.append(
                {
                    "source_capture_id": sid,
                    "source_sha256": checksum,
                    "observation_ids": observation_ids,
                    "proposal_count": count,
                    "empty_reviewed": False,
                }
            )
            inventory.append(
                {
                    "file": source.name,
                    "sha256": checksum,
                    "source_name": job["request"]["files"][source_number - 1]["name"],
                    "source_sha256": job["request"]["files"][source_number - 1][
                        "sha256"
                    ],
                    "pdf_page": None,
                    "size": list(image.size),
                }
            )
        (raw / "segmentation_manifest.jsonl").write_text(
            "".join(json.dumps(v) + "\n" for v in values), encoding="utf-8"
        )
        atomic(
            raw / "segmentation_run.json",
            {
                "capture_mode": "multi",
                "complete": True,
                "total_discovered": len(sources),
                "source_count": len(sources),
                "piece_count": sum(counts),
                "sources": sources,
            },
        )
        atomic(raw / "capture_profile.json", read(root / "capture_profile.json"))
        atomic(
            raw / "integration_receipt.json",
            {"result": verify_output(raw), "artifacts": files(raw)},
        )
        atomic(
            root / "inputs/inventory.json",
            {
                "captures": inventory,
                "pdf_dpi": 300,
                "source": {
                    p.name: sha(p) for p in (root / "inputs/original").iterdir()
                },
            },
        )
        atomic(
            raw.parent / "attempt.json",
            {"kind": "raw", "lock_sha256": settings.lock_hash},
        )
        app.state.store.update(job["id"], state="review_required", phase="stage1")
        return job, root, raw

    yield client, settings, make
    client.close()


def state(client, job):
    response = client.get(f"/api/v1/jobs/{job['id']}/review")
    assert response.status_code == 200, response.text
    return response.json()


def approve(client, job, piece, mask=None, decision="approved"):
    return client.post(
        f"/api/v1/jobs/{job['id']}/pieces/{piece['capture_id']}/review",
        data={"base_sha256": piece["base_sha256"], "decision": decision},
        files={"mask": ("mask.png", png(mask), "image/png")}
        if mask is not None
        else None,
    )


def confirm_all(client, job):
    snapshot = state(client, job)
    for source in snapshot["sources"]:
        response = client.post(
            f"/api/v1/jobs/{job['id']}/sources/{source['source_capture_id']}/confirm",
            json={
                "revision": snapshot["revision"],
                "empty": source["piece_count"] == 0,
            },
        )
        assert response.status_code == 200, response.text
        snapshot = response.json()
    return snapshot


def publish(client, settings, job, root, raw, *, expected=0, reconstruct=False):
    response = client.post(
        f"/api/v1/jobs/{job['id']}/review-revisions",
        json={
            "revision": state(client, job)["revision"],
            "expected_pieces": expected,
            "reconstruct": reconstruct,
            "acquisition_confirmed": reconstruct,
        },
    )
    assert response.status_code == 200, response.text
    reviewed = root / "attempts/0002/stage1_reviewed"
    publish_review(raw, root / "attempts/0002/review/decisions.json", reviewed)
    return reviewed


@pytest.mark.parametrize("count", [0, 1, 2, 7])
def test_zero_one_many_per_source_keep_independent_counts(scene, count):
    client, settings, make = scene
    job, root, raw = make((count,))
    snapshot = state(client, job)
    assert len(snapshot["sources"]) == 1
    assert snapshot["sources"][0]["piece_count"] == count
    for piece in snapshot["pieces"]:
        if not piece["empty_proposal"]:
            assert approve(client, job, piece).status_code == 200
    confirm_all(client, job)
    output = publish(client, settings, job, root, raw, expected=count)
    assert len(rows(output)) == count
    result = gate(output, root / "inputs/inventory.json", count)
    assert (
        result["status"] == "ready"
        and result["pieces"] == count
        and result["sources"] == 1
    )


def test_same_photo_corrections_publish_distinct_artifacts(scene):
    client, settings, make = scene
    job, root, raw = make()
    original_files = files(raw)
    snapshot = state(client, job)
    first, second = snapshot["pieces"][:2]
    mask = Image.open(raw / rows(raw)[0]["mask_path"]).convert("L")
    mask.putpixel((6, 12), 0)
    assert approve(client, job, first, mask).status_code == 200
    assert state(client, job)["pieces"][1]["base_sha256"] == second["base_sha256"]
    assert approve(client, job, second).status_code == 200
    confirm_all(client, job)
    output = publish(client, settings, job, root, raw, expected=2, reconstruct=True)
    values = rows(output)
    for field in ("mask_path", "piece_path", "piece_mask_path", "overlay_path"):
        assert len({r[field] for r in values}) == 2
    assert {r["capture_id"] for r in values} == {
        first["capture_id"],
        second["capture_id"],
    }
    assert files(raw) == original_files
    assert verify_output(output)["status"] == "ready"
    assert len(read(output / "segmentation_run.json")["sources"]) == 2
    assert gate(output, root / "inputs/inventory.json", 2)["pieces"] == 2


def test_actual_overlap_rejected_and_stale_mask_correction_refused(scene):
    client, _, make = scene
    job, _, raw = make((2,))
    snapshot = state(client, job)
    a, b = snapshot["pieces"]
    other = Image.open(raw / rows(raw)[1]["mask_path"]).convert("L")
    assert approve(client, job, a, other).status_code == 422
    own = Image.open(raw / rows(raw)[0]["mask_path"]).convert("L")
    own.putpixel((6, 12), 0)
    assert approve(client, job, a, own).status_code == 200
    assert approve(client, job, a).status_code == 409
    assert state(client, job)["pieces"][1]["base_sha256"] == b["base_sha256"]


def test_overlapping_boxes_with_disjoint_native_masks_are_valid(scene):
    client, settings, make = scene
    a, b = Image.new("L", (100, 90)), Image.new("L", (100, 90))
    da, db = ImageDraw.Draw(a), ImageDraw.Draw(b)
    da.rectangle((8, 8, 12, 50), fill=255)
    da.rectangle((8, 46, 50, 50), fill=255)
    db.rectangle((20, 15, 60, 20), fill=255)
    db.rectangle((55, 15, 60, 65), fill=255)
    job, root, raw = make((2,), masks=[a, b])
    for piece in state(client, job)["pieces"]:
        assert approve(client, job, piece).status_code == 200
    confirm_all(client, job)
    assert verify_output(publish(client, settings, job, root, raw))["pieces"] == 2


def test_source_confirmation_and_final_expected_count_are_required(scene):
    client, _, make = scene
    job, _, _ = make((1, 0), expected=7)
    snapshot = state(client, job)
    assert approve(client, job, snapshot["pieces"][0]).status_code == 200
    assert client.post(f"/api/v1/jobs/{job['id']}/review-revisions").status_code == 409
    confirm_all(client, job)
    assert (
        client.post(
            f"/api/v1/jobs/{job['id']}/review-revisions", json={"expected_pieces": 7}
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/api/v1/jobs/{job['id']}/review-revisions",
            json={"expected_pieces": 1, "reconstruct": True},
        ).status_code
        == 409
    )


def test_add_reject_restore_and_snapshot_conflicts(scene):
    client, _, make = scene
    job, _, _ = make((0,))
    snapshot = state(client, job)
    source = snapshot["sources"][0]["source_capture_id"]
    url = f"/api/v1/jobs/{job['id']}/sources/{source}/pieces"
    response = client.post(url, json={"revision": snapshot["revision"]})
    assert response.status_code == 200
    assert client.post(url, json={"revision": snapshot["revision"]}).status_code == 409
    piece = next(p for p in response.json()["pieces"] if not p["empty_proposal"])
    mask = Image.new("L", (100, 90))
    ImageDraw.Draw(mask).rectangle((20, 20, 50, 60), fill=255)
    assert approve(client, job, piece, mask).status_code == 200
    snapshot = confirm_all(client, job)
    decision = f"/api/v1/jobs/{job['id']}/pieces/{piece['capture_id']}/decision"
    response = client.post(
        decision + "?decision=rejected", json={"revision": snapshot["revision"]}
    )
    assert (
        response.status_code == 200 and not response.json()["sources"][0]["confirmed"]
    )
    assert response.json()["sources"][0]["piece_count"] == 0
    response = client.post(
        decision + "?decision=pending", json={"revision": response.json()["revision"]}
    )
    assert (
        response.status_code == 200
        and response.json()["sources"][0]["piece_count"] == 1
    )


def test_split_and_merge_preserve_parent_history_and_new_identities(scene):
    client, settings, make = scene
    bridge = Image.new("L", (100, 90))
    draw = ImageDraw.Draw(bridge)
    draw.rectangle((10, 15, 30, 65), fill=255)
    draw.rectangle((55, 15, 75, 65), fill=255)
    draw.rectangle((30, 38, 55, 42), fill=255)
    job, root, raw = make((1,), masks=[bridge])
    initial = state(client, job)["pieces"][0]
    cut = bridge.copy()
    ImageDraw.Draw(cut).rectangle((31, 38, 54, 42), fill=0)
    assert approve(client, job, initial, cut, "pending").status_code == 200
    snapshot = state(client, job)
    response = client.post(
        f"/api/v1/jobs/{job['id']}/pieces/{initial['capture_id']}/split",
        json={"revision": snapshot["revision"]},
    )
    assert response.status_code == 200, response.text
    children = [p for p in response.json()["pieces"] if p["origin"] == "split"]
    assert len(children) == 2 and all(
        p["parents"] == [initial["capture_id"]] for p in children
    )
    response = client.post(
        f"/api/v1/jobs/{job['id']}/pieces/merge",
        json={
            "revision": response.json()["revision"],
            "piece_ids": [p["capture_id"] for p in children],
        },
    )
    assert response.status_code == 200, response.text
    merged = next(p for p in response.json()["pieces"] if p["origin"] == "merge")
    assert (
        approve(client, job, merged).status_code == 422
    )  # Separate regions are still not an accepted piece.
    assert approve(client, job, merged, bridge).status_code == 200
    confirm_all(client, job)
    output = publish(client, settings, job, root, raw, expected=1)
    assert [r["capture_id"] for r in rows(output)] == [merged["capture_id"]]
    assert len(read(output / "review_history.json")["observations"]) == 4


def test_confirmed_empty_batch_finishes_before_any_downstream_stage(scene):
    client, settings, make = scene
    job, root, _ = make((0, 0))
    confirm_all(client, job)
    response = client.post(
        f"/api/v1/jobs/{job['id']}/review-revisions", json={"reconstruct": False}
    )
    assert response.status_code == 200
    claimed = client.app.state.store.claim()
    Coordinator(settings, client.app.state.store).run(claimed)
    result = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert result["state"] == "completed", result.get("detail")
    assert result["outcome"] == "no_pieces" and result["summary"]["pieces"] == 0
    assert not (root / "attempts/0002/stage2").exists()


def test_native_source_mutation_refuses_publication(scene):
    client, settings, make = scene
    job, root, raw = make((1,))
    piece = state(client, job)["pieces"][0]
    assert approve(client, job, piece).status_code == 200
    confirm_all(client, job)
    assert (
        client.post(
            f"/api/v1/jobs/{job['id']}/review-revisions", json={"reconstruct": False}
        ).status_code
        == 200
    )
    Image.new("RGB", (100, 90), "red").save(rows(raw)[0]["source_path"])
    with pytest.raises(ValueError, match="source image changed"):
        publish_review(
            raw,
            root / "attempts/0002/review/decisions.json",
            root / "attempts/0002/stage1_reviewed",
        )


def test_normalization_counts_photos_independently_of_expected_pieces(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    Image.new("RGB", (30, 20), "blue").save(source / "photo.png")
    result = normalize(source, tmp_path / "normalized", 300, 7)
    assert len(result["captures"]) == 1 and result["expected_physical_pieces"] == 7


def test_saved_segmentation_can_start_reconstruction_without_reupload(scene):
    client, settings, make = scene
    job, root, raw = make((1,))
    assert approve(client, job, state(client, job)["pieces"][0]).status_code == 200
    confirm_all(client, job)
    publish(client, settings, job, root, raw, expected=1)
    store = client.app.state.store
    Coordinator(settings, store).run(store.claim())
    result = client.get(f"/api/v1/jobs/{job['id']}").json()
    assert result["outcome"] == "segmented"
    response = client.post(
        f"/api/v1/jobs/{job['id']}/review-revisions",
        json={"reconstruct": True, "acquisition_confirmed": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["attempt"] == 3 and response.json()["state"] == "queued"
    assert read(root / "attempts/0003/attempt.json")["raw_attempt"] == 1
