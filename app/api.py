from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import uuid
import zipfile
from pathlib import Path

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from . import __version__
from .common import atomic, contained, digest, now, read, rows, sha
from .config import ROOT, Settings
from .processes import recover_locks
from .store import Store


def raw_root(settings, job):
    attempt = settings.attempt(job)
    meta = read(attempt / "attempt.json") if (attempt / "attempt.json").exists() else {}
    number = meta.get("raw_attempt", job["attempt"])
    return settings.job(job["id"]) / "attempts" / f"{number:04d}" / "stage1_native"


def artifact_id(root, path):
    return digest(Path(path).relative_to(root).as_posix())[:32]


def artifacts(settings, job):
    root = settings.job(job["id"])
    # No arbitrary path API. Every ID is derived from an existing contained file.
    result = {}
    for path in root.rglob("*"):
        if (
            path.is_file()
            and not path.is_symlink()
            and path.resolve().is_relative_to(root.resolve())
        ):
            relative = path.relative_to(root).as_posix()
            if path.name.endswith(".lock") or path.suffix in {".tmp", ".pyc"}:
                continue
            result[artifact_id(root, path)] = {
                "path": relative,
                "size": path.stat().st_size,
                "url": f"/api/v1/jobs/{job['id']}/artifacts/{artifact_id(root, path)}",
            }
    return result


def create_app(settings=None):
    settings = settings or Settings.load()
    store = Store(settings.data)
    app = FastAPI(title="Paper reconstruction", version=__version__)
    app.state.settings, app.state.store = settings, store

    @app.exception_handler(ValueError)
    async def bad_input(request, error):
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.middleware("http")
    async def local_origin(request, call_next):
        # Same-origin writes protect the local app from unrelated websites.
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            from urllib.parse import urlparse

            if urlparse(origin).netloc != request.headers.get("host"):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Cross-origin writes are disabled"},
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        return response

    def get(job_id):
        job = store.get(job_id)
        if job is None:
            raise HTTPException(404, "Job does not exist")
        return job

    def describe(job):
        attempt = settings.attempt(job)
        inventory = settings.job(job["id"]) / "inputs/inventory.json"
        return {
            **job,
            "stages": store.stages(job),
            "summary": read(attempt / "summary.json")
            if (attempt / "summary.json").exists()
            else None,
            "inventory": read(inventory) if inventory.exists() else None,
        }

    @app.get("/healthz")
    def health():
        return {"status": "alive", "version": __version__}

    @app.get("/readyz")
    def readiness():
        if not store.ready():
            raise HTTPException(503, "Coordinator heartbeat is unavailable")
        if not all(Path(p).is_file() for p in settings.runtimes.values()):
            raise HTTPException(503, "A stage runtime is missing")
        for name, component in read(settings.lock)["components"].items():
            for model in component["models"]:
                path = settings.components / name / model["path"]
                if not path.is_file() or path.stat().st_size != model["bytes"]:
                    raise HTTPException(503, "A stage model is missing")
        return {"status": "ready", "worker": "ready", "lock_sha256": settings.lock_hash}

    @app.get("/api/v1/system/versions")
    def versions():
        return {
            "application": __version__,
            "lock_sha256": settings.lock_hash,
            "lock": read(settings.lock),
            "application_sha256": settings.application_hash,
            "runtimes": read("/opt/build/runtime.json")
            if Path("/opt/build/runtime.json").exists()
            else {},
            "worker_ready": store.ready(),
            "device": "cpu",
            "concurrent_jobs": 1,
            "runtime_requirements": {
                p.name: p.read_text()
                for p in Path("/opt/build").glob("*requirements.txt")
            },
        }

    @app.get("/api/v1/jobs")
    def jobs():
        return [describe(job) for job in store.list()]

    @app.post("/api/v1/jobs", status_code=202)
    def submit(
        files: list[UploadFile] = File(...),
        name: str = Form("Untitled page"),
        review: str = Form("flagged"),
        acquisition_confirmed: bool = Form(False),
        expected_pieces: int = Form(0),
        pdf_dpi: int = Form(300),
        idempotency_key: str | None = Header(None),
    ):
        if not acquisition_confirmed:
            raise HTTPException(
                422,
                "Confirm one complete page at common native scale, without perspective or resizing",
            )
        if (
            review not in {"flagged", "all", "none"}
            or not 0 <= expected_pieces <= 60
            or not 72 <= pdf_dpi <= 600
        ):
            raise HTTPException(422, "Invalid review policy, inventory or PDF DPI")
        if (
            not 1 <= len(files) <= settings.max_files
            or not 1 <= len(name.strip()) <= 120
        ):
            raise HTTPException(422, "Provide 1–60 capture files and a short page name")
        job_id = uuid.uuid4().hex
        root = settings.job(job_id)
        originals = root / "inputs/original"
        originals.mkdir(parents=True)
        total, inventory = 0, []
        try:
            for index, upload in enumerate(files, 1):
                extension = Path(upload.filename or "").suffix.lower()
                if extension not in {
                    ".png",
                    ".jpg",
                    ".jpeg",
                    ".tif",
                    ".tiff",
                    ".bmp",
                    ".pdf",
                }:
                    raise HTTPException(422, "Use PNG, JPEG, TIFF, BMP or PDF captures")
                filename = f"capture_{index:04d}{extension}"
                path = originals / filename
                with path.open("wb") as output:
                    while block := upload.file.read(1024 * 1024):
                        total += len(block)
                        if total > settings.max_upload_bytes:
                            raise HTTPException(413, "Batch exceeds 250 MB")
                        output.write(block)
                if path.stat().st_size == 0:
                    raise HTTPException(422, "Empty capture file")
                if extension == ".pdf":
                    with path.open("rb") as stream:
                        if not stream.read(8).startswith(b"%PDF-"):
                            raise HTTPException(422, "File is not a PDF")
                else:
                    try:
                        with Image.open(path) as image:
                            if image.width * image.height > settings.max_pixels:
                                raise HTTPException(
                                    413, "Image exceeds native pixel limit"
                                )
                            if getattr(image, "n_frames", 1) != 1:
                                raise HTTPException(
                                    422, "Use separate files for multi-frame images"
                                )
                            image.verify()
                    except HTTPException:
                        raise
                    except Exception as error:
                        raise HTTPException(422, "File is not a valid image") from error
                inventory.append(
                    {
                        "name": filename,
                        "original_name": (upload.filename or filename)[:200],
                        "bytes": path.stat().st_size,
                        "sha256": sha(path),
                    }
                )
            if len({item["sha256"] for item in inventory}) != len(inventory):
                raise HTTPException(
                    422,
                    "Duplicate capture files would collapse physical-piece identity",
                )
            request = {
                "schema_version": "orchestrator.request.v1",
                "name": name.strip(),
                "files": inventory,
                "review": review,
                "pdf_dpi": pdf_dpi,
                "expected_pieces": expected_pieces,
                "cpu_threads": settings.threads,
                "lock_sha256": settings.lock_hash,
                "application_sha256": settings.application_hash,
                "acquisition_confirmed": True,
                "capture_profile_sha256": sha(
                    ROOT / "composition/profiles/capture.json"
                ),
            }
            previous = store.by_key(idempotency_key) if idempotency_key else None
            if previous:
                if request != previous["request"]:
                    raise HTTPException(
                        409, "Idempotency key was used for different inputs"
                    )
                shutil.rmtree(root)
                return describe(previous)
            atomic(root / "request.json", request)
            shutil.copyfile(settings.lock, root / "pipeline.lock.json")
            shutil.copyfile(
                ROOT / "composition/profiles/capture.json",
                root / "capture_profile.json",
            )
            return describe(
                store.create(job_id, name.strip(), request, idempotency_key)
            )
        except BaseException:
            if root.exists() and store.get(job_id) is None:
                shutil.rmtree(root)
            raise
        finally:
            for upload in files:
                upload.file.close()

    @app.get("/api/v1/jobs/{job_id}")
    def detail(job_id: str):
        return describe(get(job_id))

    @app.post("/api/v1/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        job = get(job_id)
        if job["state"] in {"queued", "review_required", "interrupted"}:
            store.transition(
                job_id,
                {job["state"]},
                "cancelled",
                cancelled=1,
                detail="Cancelled by user; artifacts and review drafts were retained",
            )
        elif job["state"] in {"running", "cancelling"}:
            store.transition(
                job_id, {"running", "cancelling"}, "cancelling", cancelled=1
            )
        else:
            raise HTTPException(409, "This job is already terminal")
        return describe(get(job_id))

    @app.post("/api/v1/jobs/{job_id}/resume")
    def resume(job_id: str):
        job = get(job_id)
        if job["state"] not in {"failed", "interrupted", "cancelled"}:
            raise HTTPException(
                409, "Only interrupted, failed or cancelled jobs can resume"
            )
        if job["request"]["lock_sha256"] != settings.lock_hash:
            raise HTTPException(
                409, "Restore this job's original image before resuming"
            )
        if job["request"].get("application_sha256") != settings.application_hash:
            raise HTTPException(
                409, "Restore this job's original application build before resuming"
            )
        recover_locks(settings.attempt(job))
        store.transition(
            job_id, {job["state"]}, "queued", cancelled=0, detail=None, outcome=None
        )
        return describe(get(job_id))

    @app.get("/api/v1/jobs/{job_id}/pieces")
    def pieces(job_id: str):
        job = get(job_id)
        source = raw_root(settings, job)
        root = settings.job(job_id)
        result = []
        for row in rows(source):
            entry = {
                k: row.get(k)
                for k in (
                    "capture_id",
                    "accepted",
                    "review_status",
                    "reason_codes",
                    "original_size_hw",
                    "bbox_xywh",
                )
            }
            entry["base_sha256"] = sha(contained(source, row["mask_path"]))
            for label, path in [
                ("image", Path(row["source_path"])),
                ("mask", source / row["mask_path"]),
                ("overlay", source / row["overlay_path"]),
            ]:
                if not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError("Piece source escapes job")
                entry[label] = (
                    f"/api/v1/jobs/{job_id}/artifacts/{artifact_id(root, path)}"
                )
            draft = (
                settings.attempt(job) / "review/drafts" / (row["capture_id"] + ".json")
            )
            entry["approved"] = draft.exists()
            if draft.exists():
                correction = read(draft)
                mask_path = settings.attempt(job) / "review" / correction["mask"]
                entry["edited_mask"] = (
                    f"/api/v1/jobs/{job_id}/artifacts/{artifact_id(root, mask_path)}"
                )
            result.append(entry)
        return result

    @app.post("/api/v1/jobs/{job_id}/pieces/{capture_id}/review")
    def review_piece(
        job_id: str,
        capture_id: str,
        base_sha256: str = Form(...),
        mask: UploadFile | None = File(None),
    ):
        job = get(job_id)
        if job["state"] != "review_required":
            raise HTTPException(409, "Job is not awaiting review")
        row = next(
            (r for r in rows(raw_root(settings, job)) if r["capture_id"] == capture_id),
            None,
        )
        if row is None:
            raise HTTPException(404, "Unknown piece")
        original = contained(raw_root(settings, job), row["mask_path"])
        if base_sha256 != sha(original):
            raise HTTPException(409, "Mask revision changed; reload it")
        review_dir = settings.attempt(job) / "review"
        masks = review_dir / "masks"
        masks.mkdir(parents=True, exist_ok=True)
        temporary = masks / (uuid.uuid4().hex + ".png")
        try:
            if mask:
                payload = mask.file.read(25 * 1024 * 1024 + 1)
                if len(payload) > 25 * 1024 * 1024:
                    raise HTTPException(413, "Correction exceeds 25 MB")
                temporary.write_bytes(payload)
            else:
                shutil.copyfile(original, temporary)
            checked = subprocess.run(
                [
                    settings.python("stage1"),
                    str(ROOT / "tools/stage_io.py"),
                    "mask",
                    "--input",
                    str(temporary),
                    "--shape",
                    *map(str, row["original_size_hw"]),
                ],
                capture_output=True,
                text=True,
                env=settings.environment("stage1"),
                timeout=45,
            )
            if checked.returncode:
                raise HTTPException(422, (checked.stderr or checked.stdout)[-1000:])
            checksum = sha(temporary)
            target = masks / (checksum + ".png")
            temporary.replace(target)
            correction = {
                "capture_id": capture_id,
                "base_sha256": base_sha256,
                "sha256": checksum,
                "mask": target.relative_to(review_dir).as_posix(),
                "decision": "approved",
                "at": now(),
            }
            atomic(review_dir / "drafts" / (capture_id + ".json"), correction)
            store.event(
                job_id,
                "review",
                {
                    "capture_id": capture_id,
                    "decision": "approved",
                    "mask_sha256": checksum,
                },
            )
            return correction
        finally:
            temporary.unlink(missing_ok=True)
            if mask:
                mask.file.close()

    @app.post("/api/v1/jobs/{job_id}/review-revisions")
    def apply_review(job_id: str):
        job = get(job_id)
        if job["state"] != "review_required":
            raise HTTPException(409, "Job is not awaiting review")
        previous = settings.attempt(job)
        review_dir = previous / "review"
        submitted = (
            read(review_dir / "decisions.json")
            if (review_dir / "decisions.json").exists()
            else {}
        )
        submitted.update(
            {p.stem: read(p) for p in (review_dir / "drafts").glob("*.json")}
        )
        values = rows(raw_root(settings, job))
        if not submitted or any(
            not row["accepted"] and row["capture_id"] not in submitted for row in values
        ):
            raise HTTPException(409, "Approve every pending physical piece first")
        next_attempt = previous.parent / f"{job['attempt'] + 1:04d}"
        if next_attempt.exists():
            raise HTTPException(
                409,
                "An unpublished review revision already exists; inspect it before recovery",
            )
        target = next_attempt / "review/masks"
        target.mkdir(parents=True)
        for correction in submitted.values():
            source = contained(review_dir, correction["mask"])
            if sha(source) != correction["sha256"]:
                raise ValueError("Review draft changed")
            shutil.copyfile(source, target / source.name)
        atomic(next_attempt / "review/decisions.json", submitted)
        raw_attempt = int(raw_root(settings, job).parent.name)
        atomic(
            next_attempt / "attempt.json",
            {
                "kind": "review",
                "raw_attempt": raw_attempt,
                "lock_sha256": settings.lock_hash,
                "created": now(),
            },
        )
        store.transition(
            job_id,
            {"review_required"},
            "queued",
            attempt=job["attempt"] + 1,
            detail=None,
            phase=None,
        )
        return describe(get(job_id))

    @app.get("/api/v1/jobs/{job_id}/events")
    async def events(job_id: str, request: Request, after: int = 0):
        get(job_id)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(422, "Invalid event cursor")

        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                batch = store.events(job_id, cursor)
                if batch:
                    for event in batch:
                        cursor = event["id"]
                        yield f"id: {cursor}\nevent: progress\ndata: {json.dumps(event)}\n\n"
                else:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/v1/jobs/{job_id}/assembly-attempts")
    def assembly_attempt(job_id: str):
        job = get(job_id)
        if job["state"] not in {"completed", "failed", "interrupted", "cancelled"}:
            raise HTTPException(
                409, "Stop execution before starting a new assembly attempt"
            )
        if (
            job["request"]["lock_sha256"] != settings.lock_hash
            or job["request"].get("application_sha256") != settings.application_hash
        ):
            raise HTTPException(
                409, "Restore the original image before reusing this job's evidence"
            )
        current = settings.attempt(job)
        metadata = (
            read(current / "attempt.json")
            if (current / "attempt.json").exists()
            else {}
        )
        evidence_attempt = metadata.get("evidence_attempt", job["attempt"])
        evidence = current.parent / f"{evidence_attempt:04d}"
        bundle, capture = (
            evidence / "stage3/bundle/bundle.json",
            evidence / "stage1/capture_profile.json",
        )
        if not bundle.is_file() or not capture.is_file():
            raise HTTPException(409, "A published Stage 3 bundle is required")
        recover_locks(current)
        next_attempt = current.parent / f"{job['attempt'] + 1:04d}"
        if next_attempt.exists():
            raise HTTPException(409, "An unpublished attempt needs inspection")
        atomic(
            next_attempt / "attempt.json",
            {
                "kind": "assembly",
                "evidence_attempt": evidence_attempt,
                "raw_attempt": int(raw_root(settings, job).parent.name),
                "bundle_sha256": sha(bundle),
                "capture_sha256": sha(capture),
                "lock_sha256": settings.lock_hash,
                "created": now(),
            },
        )
        store.transition(
            job_id,
            {job["state"]},
            "queued",
            attempt=job["attempt"] + 1,
            cancelled=0,
            detail=None,
            outcome=None,
            phase=None,
        )
        return describe(get(job_id))

    @app.get("/api/v1/jobs/{job_id}/artifacts")
    def artifact_list(job_id: str):
        return artifacts(settings, get(job_id))

    @app.get("/api/v1/jobs/{job_id}/artifacts/{item_id}")
    def download(job_id: str, item_id: str):
        job = get(job_id)
        item = artifacts(settings, job).get(item_id)
        if item is None:
            raise HTTPException(404, "Artifact does not exist")
        path = contained(settings.job(job_id), item["path"])
        return FileResponse(
            path, media_type="image/png" if path.suffix == ".png" else None
        )

    @app.get("/api/v1/jobs/{job_id}/export")
    def export(job_id: str):
        job = get(job_id)
        if job["state"] in {"queued", "running", "cancelling"}:
            raise HTTPException(409, "Wait for execution to stop before exporting")
        root = settings.job(job_id)
        export_dir = settings.data / "exports"
        export_dir.mkdir(exist_ok=True)
        archive = tempfile.NamedTemporaryFile(
            suffix=".zip", dir=export_dir, delete=False
        )
        archive.close()
        with zipfile.ZipFile(
            archive.name, "w", compression=zipfile.ZIP_DEFLATED
        ) as output:
            for item in artifacts(settings, job).values():
                path = contained(root, item["path"])
                output.write(path, item["path"])
            history = []
            while batch := store.events(job_id, history[-1]["id"] if history else 0):
                history.extend(batch)
            output.writestr("application_events.json", json.dumps(history, indent=2))
        from starlette.background import BackgroundTask

        return FileResponse(
            archive.name,
            filename=f"paper-{job_id}.zip",
            background=BackgroundTask(Path(archive.name).unlink),
        )

    web = ROOT / "web/dist"
    if web.exists():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app


app = create_app()
