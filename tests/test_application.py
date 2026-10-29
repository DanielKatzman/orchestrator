import io
import sys
import threading

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.api import create_app
from app.common import contained, read
from app.config import ROOT, Settings
from app.store import Store
from app.worker import Cancelled, Coordinator, NeedsReview


@pytest.fixture
def configured(tmp_path):
    return Settings(
        tmp_path,
        ROOT / "composition/pipeline.lock.json",
        tmp_path / "components",
        {s: sys.executable for s in ("stage1", "stage2", "stage3", "stage4")},
        cancel_grace=2,
    )


@pytest.fixture
def client(configured):
    with TestClient(create_app(configured)) as value:
        yield value


def picture(color):
    out = io.BytesIO()
    Image.new("RGB", (40, 40), color).save(out, "PNG")
    return out.getvalue()


def submit(client, **kw):
    return client.post(
        "/api/v1/jobs",
        data={"name": "Page", "acquisition_confirmed": "true", **kw},
        files=[
            ("files", ("a.png", picture("red"), "image/png")),
            ("files", ("b.png", picture("blue"), "image/png")),
        ],
    )


def test_submission_freezes_inputs_and_lock(client, configured):
    response = submit(client)
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["state"] == "queued"
    assert read(configured.job(job["id"]) / "request.json") == job["request"]
    assert (
        configured.job(job["id"]) / "pipeline.lock.json"
    ).read_bytes() == configured.lock.read_bytes()
    assert len(client.get(f"/api/v1/jobs/{job['id']}/artifacts").json()) == 5


def test_undeclared_acquisition_rejected(client):
    response = submit(client, acquisition_confirmed="false")
    assert response.status_code == 422
    assert client.get("/api/v1/jobs").json() == []


def test_duplicate_piece_is_not_silently_dropped(client):
    payload = picture("red")
    response = client.post(
        "/api/v1/jobs",
        data={"acquisition_confirmed": "true"},
        files=[("files", ("a.png", payload)), ("files", ("renamed.png", payload))],
    )
    assert response.status_code == 422
    assert client.get("/api/v1/jobs").json() == []


def test_file_contents_are_checked(client):
    response = client.post(
        "/api/v1/jobs",
        data={"acquisition_confirmed": "true"},
        files=[("files", ("a.png", b"not an image"))],
    )
    assert response.status_code == 422


def test_idempotent_submission_and_conflicting_reuse(client):
    body = {"acquisition_confirmed": "true"}
    files = [("files", ("a.png", picture("red")))]
    head = {"Idempotency-Key": "my-page"}
    first = client.post("/api/v1/jobs", data=body, files=files, headers=head)
    second = client.post("/api/v1/jobs", data=body, files=files, headers=head)
    assert first.json()["id"] == second.json()["id"]
    bad = client.post(
        "/api/v1/jobs", data={**body, "name": "Changed"}, files=files, headers=head
    )
    assert bad.status_code == 409
    assert len(client.get("/api/v1/jobs").json()) == 1


def test_cancellation_resume_and_state_guards(client, configured):
    job_id = submit(client).json()["id"]
    assert client.post(f"/api/v1/jobs/{job_id}/review-revisions").status_code == 409
    assert client.post(f"/api/v1/jobs/{job_id}/cancel").json()["state"] == "cancelled"
    assert client.post(f"/api/v1/jobs/{job_id}/resume").json()["state"] == "queued"
    assert client.post(f"/api/v1/jobs/{job_id}/resume").status_code == 409


def test_changed_lock_refuses_resume(client, configured):
    job_id = submit(client).json()["id"]
    client.post(f"/api/v1/jobs/{job_id}/cancel")
    request = client.app.state.store.get(job_id)["request"]
    request["lock_sha256"] = "wrong"
    with client.app.state.store.connect() as db:
        import json

        db.execute(
            "UPDATE jobs SET request=? WHERE id=?", (json.dumps(request), job_id)
        )
    assert client.post(f"/api/v1/jobs/{job_id}/resume").status_code == 409


def test_path_containment_and_artifact_ids(client, tmp_path):
    for bad in ("../outside", "/etc/passwd", "C:/outside", "x\\y"):
        with pytest.raises(ValueError):
            contained(tmp_path, bad)
    job_id = submit(client).json()["id"]
    assert client.get(f"/api/v1/jobs/{job_id}/artifacts/unknown").status_code == 404
    assert client.get("/api/v1/jobs/nonexistent").status_code == 404


def test_cross_origin_writes_refused(client):
    assert (
        client.post(
            "/api/v1/jobs", headers={"Origin": "https://unrelated.example"}
        ).status_code
        == 403
    )


def test_worker_claim_is_exclusive_and_events_replay(configured):
    store = Store(configured.data)
    store.create("a", "Page", {})
    claimed = []
    threads = [
        threading.Thread(target=lambda: claimed.append(Store(configured.data).claim()))
        for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(j is not None for j in claimed) == 1
    initial = store.events("a")
    store.event("a", "log", {"line": "still running"})
    assert len(store.events("a", initial[-1]["id"])) == 1


def test_process_review_and_failure_are_distinct(configured):
    store = Store(configured.data)
    job = store.create("process", "Page", {})
    worker = Coordinator(configured, store)
    with pytest.raises(NeedsReview):
        worker.command(
            job, "pending", "stage1", ["-c", "raise SystemExit(3)"], review=True
        )
    with pytest.raises(RuntimeError):
        worker.command(
            job, "error", "stage1", ["-c", "raise SystemExit(3)"], review=False
        )


def test_running_process_cancellation_retains_diagnostics(configured):
    store = Store(configured.data)
    job = store.create("cancel", "Page", {})
    worker = Coordinator(configured, store)
    timer = threading.Timer(0.5, lambda: store.update(job["id"], cancelled=1))
    timer.start()
    try:
        with pytest.raises(Cancelled):
            worker.command(
                job,
                "slow",
                "stage1",
                ["-c", "import time; print('started',flush=True); time.sleep(30)"],
            )
    finally:
        timer.cancel()
    record = read(configured.attempt(job) / "logs/slow.process.json")
    assert "finished" in record
    assert store.stages(job)[0]["receipt"]["returncode"] != 0


def test_recovery_understands_pid_markers_and_preserves_advisory_locks(tmp_path):
    from app.common import atomic
    from app.processes import recover_locks

    atomic(tmp_path / "logs/stage3.process.json", {"pid": 987654, "finished": "done"})
    atomic(
        tmp_path / "stage3/native.work/audit.lock",
        {"pid": 987654, "created_utc": "before"},
    )
    advisory = tmp_path / "stage2/inference.lock"
    advisory.parent.mkdir()
    advisory.write_text("0")
    recover_locks(tmp_path)
    assert not (tmp_path / "stage3/native.work/audit.lock").exists()
    assert advisory.read_text() == "0"


def test_assembly_rerun_requires_published_evidence(client):
    job_id = submit(client).json()["id"]
    assert client.post(f"/api/v1/jobs/{job_id}/assembly-attempts").status_code == 409
    client.post(f"/api/v1/jobs/{job_id}/cancel")
    assert client.post(f"/api/v1/jobs/{job_id}/assembly-attempts").status_code == 409


def test_verbose_stage_output_has_bounded_progress_events(configured):
    store = Store(configured.data)
    job = store.create("verbose", "Page", {})
    worker = Coordinator(configured, store)
    worker.command(job, "stage3", "stage3", ["-c", "for i in range(5000): print(i)"])
    assert len(store.events(job["id"])) < 20
    log = (configured.attempt(job) / "logs/stage3.log").read_text()
    assert "4999" in log


def test_accepted_cancel_wins_over_late_publication(configured):
    store = Store(configured.data)
    store.create("race", "Page", {})
    store.claim()
    store.transition("race", {"running"}, "cancelling", cancelled=1)
    store.finish("race", "completed", outcome="supported", phase="finished")
    job = store.get("race")
    assert job["state"] == "cancelled" and job["outcome"] is None


def test_cancel_after_publication_is_refused(configured):
    store = Store(configured.data)
    store.create("race", "Page", {})
    store.claim()
    store.finish("race", "completed", outcome="supported", phase="finished")
    with pytest.raises(ValueError, match="changed state"):
        store.transition("race", {"running"}, "cancelling", cancelled=1)
    assert store.get("race")["outcome"] == "supported"
