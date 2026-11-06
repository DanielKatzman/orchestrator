from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path

from fastapi import (
    Body,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field

from . import __version__
from .common import atomic, contained, digest, now, read, rows, sha
from .config import ROOT, Settings
from .processes import recover_locks
from .review import Review, review_lock
from .store import Store


class ReviewAction(BaseModel):
    revision: str
    empty: bool = False
    piece_ids: list[str] = Field(default_factory=list)


class ReviewContinuation(BaseModel):
    revision: str | None = None
    reconstruct: bool = False
    acquisition_confirmed: bool = False
    expected_pieces: int | None = Field(None, ge=0, le=60)


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
        piece_inventory = None
        for candidate in (
            attempt / "review/state.json",
            attempt / "review/decisions.json",
        ):
            if candidate.is_file():
                state = read(candidate)
                if state.get("schema_version") == "orchestrator.instance-review.v2":
                    active = [
                        p
                        for p in state["observations"].values()
                        if not p["empty_proposal"]
                        and p["decision"] not in {"rejected", "superseded"}
                    ]
                    piece_inventory = {
                        "sources": len(state["sources"]),
                        "pieces": len(active),
                        "approved": sum(p["decision"] == "approved" for p in active),
                    }
                    break
        return {
            **job,
            "stages": store.stages(job),
            "summary": read(attempt / "summary.json")
            if (attempt / "summary.json").exists()
            else None,
            "inventory": read(inventory) if inventory.exists() else None,
            "piece_inventory": piece_inventory,
            "requires_original_image": (
                job["request"].get("application_sha256") != settings.application_hash
                or job["request"]["lock_sha256"] != settings.lock_hash
            ),
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
        review: str = Form("all"),
        acquisition_confirmed: bool = Form(False),
        expected_pieces: int = Form(0),
        pdf_dpi: int = Form(300),
        idempotency_key: str | None = Header(None),
    ):
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
            raise HTTPException(
                422, "Provide 1ג€“60 capture files and a short page name"
            )
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
                "acquisition_confirmed": acquisition_confirmed,
                "piece_mode": "multi",
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
        with review_lock(job_id):
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

    def review_payload(job):
        review = Review(settings, job)
        if review.state is None:
            return {"revision": "", "sources": [], "pieces": []}
        root = settings.job(job["id"])

        def url(path):
            if not Path(path).resolve().is_relative_to(root.resolve()):
                raise ValueError("Review artifact escapes job")
            return f"/api/v1/jobs/{job['id']}/artifacts/{artifact_id(root, path)}"

        pieces = []
        original_ids = {r["capture_id"] for r in rows(review.raw)}
        published_root = review.folder.parent / "stage1_reviewed"
        published = {r["capture_id"]: r for r in rows(published_root)}
        source_order = review.state.get("source_order", list(review.state["sources"]))
        source_index = {sid: i for i, sid in enumerate(source_order)}
        for pid, item in sorted(
            review.state["observations"].items(),
            key=lambda pair: (
                source_index[review.source_id(pair[1])],
                pair[1].get("order", 0),
                pair[0],
            ),
        ):
            row = item["row"]
            current = review.mask_path(item)
            raw_mask = (
                contained(review.raw, row["mask_path"])
                if pid in original_ids
                else current
            )
            pieces.append(
                {
                    **{
                        k: row.get(k)
                        for k in (
                            "capture_id",
                            "source_capture_id",
                            "candidate_id",
                            "artifact_id",
                            "reason_codes",
                            "original_size_hw",
                            "bbox_xywh",
                        )
                    },
                    "source_capture_id": review.source_id(item),
                    "accepted": item["decision"] == "approved",
                    "approved": item["decision"] == "approved",
                    "decision": item["decision"],
                    "empty_proposal": item["empty_proposal"],
                    "origin": item["origin"],
                    "parents": item["parents"],
                    "base_sha256": item["sha256"],
                    "image": url(Path(row["source_path"])),
                    "mask": url(raw_mask),
                    "edited_mask": url(current),
                    "overlay": url(current),
                }
            )
            if pid in published:
                pieces[-1]["piece"] = url(
                    contained(published_root, published[pid]["piece_path"])
                )
        sources = []
        inventory = root / "inputs/inventory.json"
        captures = read(inventory).get("captures", []) if inventory.exists() else []
        names = {
            p["sha256"]: p["source_name"]
            + (f" · page {p['pdf_page']}" if p.get("pdf_page") else "")
            for p in captures
        }
        for sid in source_order:
            source = review.state["sources"][sid]
            row = source["row_template"]
            active = [
                p
                for p in pieces
                if p["source_capture_id"] == sid
                and p["decision"] not in {"rejected", "superseded"}
                and not p["empty_proposal"]
            ]
            sources.append(
                {
                    "source_capture_id": sid,
                    "source_sha256": source["source_sha256"],
                    "name": names.get(
                        source["source_sha256"], Path(row["source_path"]).name
                    ),
                    "image": url(Path(row["source_path"])),
                    "original_size_hw": row["original_size_hw"],
                    "confirmed": source["confirmed"],
                    "empty_reviewed": source["empty_reviewed"],
                    "piece_count": len(active),
                    "approved_count": sum(p["approved"] for p in active),
                    "proposal_count": source.get("proposal_count", 0),
                }
            )
        return {"revision": review.revision, "sources": sources, "pieces": pieces}

    @contextmanager
    def editing(job_id, revision=None, saved=False):
        with review_lock(job_id):
            job = get(job_id)
            if job["state"] != "review_required" and not (
                saved and job["state"] == "completed" and job["outcome"] == "segmented"
            ):
                raise HTTPException(409, "Job is not awaiting review")
            if (
                job["request"].get("application_sha256") != settings.application_hash
                or job["request"]["lock_sha256"] != settings.lock_hash
            ):
                raise HTTPException(
                    409, "Restore the original job image before editing its review"
                )
            review = Review(settings, job)
            if review.state is None:
                raise HTTPException(409, "Stage 1 proposals are not published yet")
            review.check_revision(revision)
            yield job, review

    @app.get("/api/v1/jobs/{job_id}/review")
    def review_state(job_id: str):
        with review_lock(job_id):
            return review_payload(get(job_id))

    @app.get("/api/v1/jobs/{job_id}/pieces")
    def pieces(job_id: str):
        with review_lock(job_id):
            return review_payload(get(job_id))["pieces"]

    @app.post("/api/v1/jobs/{job_id}/pieces/{capture_id}/review")
    def review_piece(
        job_id: str,
        capture_id: str,
        base_sha256: str = Form(...),
        revision: str | None = Form(None),
        decision: str = Form("approved"),
        mask: UploadFile | None = File(None),
    ):
        try:
            payload = mask.file.read(25 * 1024 * 1024 + 1) if mask else None
            if payload is not None and len(payload) > 25 * 1024 * 1024:
                raise HTTPException(413, "Correction exceeds 25 MB")
            with editing(job_id, revision) as (job, review):
                try:
                    review.edit(capture_id, base_sha256, payload, decision)
                except (ValueError, OSError) as error:
                    raise HTTPException(
                        422 if "changed" not in str(error) else 409, str(error)
                    ) from error
                store.event(
                    job_id, "review", {"capture_id": capture_id, "decision": decision}
                )
                return review_payload(job)
        finally:
            if mask:
                mask.file.close()

    @app.post("/api/v1/jobs/{job_id}/sources/{source_id}/pieces")
    def add_piece(job_id: str, source_id: str, action: ReviewAction):
        with editing(job_id, action.revision) as (job, review):
            ident = review.add(source_id)
            review.save("added", {"piece_id": ident})
            store.event(job_id, "review", {"capture_id": ident, "decision": "added"})
            return {**review_payload(job), "selected_piece_id": ident}

    @app.post("/api/v1/jobs/{job_id}/sources/{source_id}/confirm")
    def confirm_source(job_id: str, source_id: str, action: ReviewAction):
        with editing(job_id, action.revision) as (job, review):
            review.confirm(source_id, action.empty)
            store.event(
                job_id,
                "review",
                {
                    "source_capture_id": source_id,
                    "decision": "empty_confirmed"
                    if action.empty
                    else "source_confirmed",
                },
            )
            return review_payload(job)

    @app.post("/api/v1/jobs/{job_id}/pieces/{piece_id}/decision")
    def decide_piece(job_id: str, piece_id: str, action: ReviewAction, decision: str):
        with editing(job_id, action.revision) as (job, review):
            review.decide(piece_id, decision)
            store.event(
                job_id, "review", {"capture_id": piece_id, "decision": decision}
            )
            return review_payload(job)

    @app.post("/api/v1/jobs/{job_id}/pieces/{piece_id}/split")
    def split_piece(job_id: str, piece_id: str, action: ReviewAction):
        with editing(job_id, action.revision) as (job, review):
            ids = review.split(piece_id)
            store.event(
                job_id,
                "review",
                {"capture_id": piece_id, "decision": "split", "children": ids},
            )
            return {**review_payload(job), "selected_piece_id": ids[0]}

    @app.post("/api/v1/jobs/{job_id}/pieces/merge")
    def merge_pieces(job_id: str, action: ReviewAction):
        with editing(job_id, action.revision) as (job, review):
            ident = review.merge(action.piece_ids)
            store.event(
                job_id,
                "review",
                {
                    "capture_id": ident,
                    "decision": "merged",
                    "parents": action.piece_ids,
                },
            )
            return {**review_payload(job), "selected_piece_id": ident}

    @app.post("/api/v1/jobs/{job_id}/review-revisions")
    def apply_review(job_id: str, options: ReviewContinuation | None = Body(None)):
        with editing(
            job_id,
            options.revision if options else None,
            saved=bool(options and options.reconstruct),
        ) as (job, review):
            reconstruct = (
                options.reconstruct
                if options
                else job["request"]["acquisition_confirmed"]
            )
            confirmed = (
                options.acquisition_confirmed
                if options
                else job["request"]["acquisition_confirmed"]
            )
            previous = settings.attempt(job)
            target = previous.parent / f"{job['attempt'] + 1:04d}"
            expected = (
                options.expected_pieces
                if options and options.expected_pieces is not None
                else job["request"]["expected_pieces"]
            )
            review.freeze(target, expected, reconstruct, confirmed)
            atomic(
                target / "attempt.json",
                {
                    "kind": "review",
                    "raw_attempt": int(raw_root(settings, job).parent.name),
                    "reconstruct": reconstruct,
                    "acquisition_confirmed": confirmed,
                    "lock_sha256": settings.lock_hash,
                    "review_sha256": sha(target / "review/decisions.json"),
                    "expected_pieces": expected,
                    "created": now(),
                },
            )
            try:
                store.transition(
                    job_id,
                    {job["state"]},
                    "queued",
                    attempt=job["attempt"] + 1,
                    detail=None,
                    phase=None,
                    outcome=None,
                )
            except BaseException:
                shutil.rmtree(target)
                raise
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
                "expected_pieces": metadata.get(
                    "expected_pieces", job["request"]["expected_pieces"]
                ),
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
