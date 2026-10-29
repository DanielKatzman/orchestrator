"""Qualify the real HTTP/coordinator/stage path; no network except loopback."""

import argparse
import json
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.request import Request, urlopen

from .common import atomic, now, sha
from .config import ROOT, Settings


def multipart(fields, files=()):
    boundary = "paper" + uuid.uuid4().hex
    chunks = []
    for key, value in fields.items():
        chunks.extend(
            [
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
            ]
        )
    for key, path in files:
        chunks.extend(
            [
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{path.name}"\r\nContent-Type: image/png\r\n\r\n'.encode(),
                path.read_bytes(),
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), {
        "Content-Type": "multipart/form-data; boundary=" + boundary
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--review", choices=["flagged", "all"], default="flagged")
    parser.add_argument("--url")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--cancel-stage", choices=["stage3"])
    args = parser.parse_args()
    url = args.url or "http://127.0.0.1:8000"
    supervisor = None
    started = time.monotonic()
    receipt = {
        "started": now(),
        "review": args.review,
        "device": "cpu",
        "network": "loopback-only HTTP; no external stage fetch",
    }

    def request(path, method="GET", body=None, headers=None):
        with urlopen(
            Request(url + path, data=body, headers=headers or {}, method=method),
            timeout=60,
        ) as response:
            return json.load(response)

    try:
        if not args.url:
            supervisor = subprocess.Popen([sys.executable, "-m", "app.supervisor"])
        for _ in range(60):
            try:
                request("/readyz")
                break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError("Application did not become ready")
        receipt["installed"] = request("/api/v1/system/versions")
        files = sorted((ROOT / "tests/fixtures/captures").glob("*.png"))
        body, headers = multipart(
            {
                "name": "Four-piece CPU control"
                + (" · mask review" if args.review == "all" else ""),
                "acquisition_confirmed": "true",
                "review": args.review,
                "expected_pieces": "4",
            },
            [("files", p) for p in files],
        )
        job = request("/api/v1/jobs", "POST", body, headers)
        receipt["job_id"] = job["id"]
        receipt["capture_files"] = {p.name: sha(p) for p in files}
        last = None
        reviewed = False
        cancelled_once = False
        awaiting_cancel = False
        phase_started = time.monotonic()
        while time.monotonic() - started < args.timeout:
            job = request(f"/api/v1/jobs/{job['id']}")
            marker = (job["state"], job["phase"], job["attempt"])
            if marker != last:
                phase_started = time.monotonic()
                print(
                    json.dumps(
                        {
                            "job_id": job["id"],
                            "state": marker[0],
                            "phase": marker[1],
                            "attempt": marker[2],
                        }
                    ),
                    flush=True,
                )
                last = marker
            if (
                args.cancel_stage
                and job["phase"] == args.cancel_stage
                and job["state"] == "running"
                and not cancelled_once
                and time.monotonic() - phase_started > 15
            ):
                request(f"/api/v1/jobs/{job['id']}/cancel", "POST")
                awaiting_cancel = True
                cancelled_once = True
                continue
            if job["state"] == "cancelled" and awaiting_cancel:
                request(f"/api/v1/jobs/{job['id']}/resume", "POST")
                awaiting_cancel = False
                receipt["cancellation_resume_verified"] = True
                continue
            if job["state"] == "review_required":
                if args.review != "all" or reviewed:
                    raise RuntimeError(
                        "Unexpected review gate: " + str(job.get("detail"))
                    )
                for index, piece in enumerate(
                    request(f"/api/v1/jobs/{job['id']}/pieces")
                ):
                    uploads = []
                    if index == 0:
                        # Exercise an actual one-native-pixel correction, not
                        # merely accepting unchanged predictions.
                        import io

                        from PIL import Image

                        with urlopen(url + piece["mask"]) as response:
                            mask = Image.open(io.BytesIO(response.read())).convert("L")
                        pixels = mask.load()
                        changed = False
                        for y in range(1, mask.height - 1):
                            for x in range(1, mask.width - 1):
                                neighbors = sum(
                                    pixels[x + dx, y + dy] > 127
                                    for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1))
                                )
                                if pixels[x, y] > 127 and neighbors == 3:
                                    pixels[x, y] = 0
                                    changed = True
                                    break
                            if changed:
                                break
                        if not changed:
                            raise RuntimeError(
                                "Could not create a bounded native-pixel review correction"
                            )
                        correction = Settings.load().data / "smoke-corrected-mask.png"
                        mask.save(correction)
                        uploads = [("mask", correction)]
                        receipt["corrected_native_pixels"] = 1
                    data, head = multipart(
                        {"base_sha256": piece["base_sha256"]}, uploads
                    )
                    request(
                        f"/api/v1/jobs/{job['id']}/pieces/{piece['capture_id']}/review",
                        "POST",
                        data,
                        head,
                    )
                request(f"/api/v1/jobs/{job['id']}/review-revisions", "POST")
                reviewed = True
            elif job["state"] in {"failed", "cancelled", "interrupted"}:
                raise RuntimeError(f"Pipeline {job['state']}: {job['detail']}")
            elif job["state"] == "completed":
                if (
                    job["outcome"] != "supported"
                    or job["summary"]["pieces"] != 4
                    or not job["summary"]["connected"]
                ):
                    raise RuntimeError(
                        "The control did not reach supported four-piece connectivity"
                    )
                if args.review == "all" and (not reviewed or job["attempt"] != 2):
                    raise RuntimeError("Review route was not exercised")
                receipt.update(
                    status="passed",
                    summary=job["summary"],
                    stages=job["stages"],
                    reviewed=reviewed,
                    seconds=round(time.monotonic() - started, 3),
                    finished=now(),
                )
                output = args.output or Settings.load().data / (
                    "qualification-review.json" if reviewed else "qualification.json"
                )
                atomic(output, receipt)
                print(
                    json.dumps(
                        {
                            "status": "passed",
                            "job_id": job["id"],
                            "summary": job["summary"],
                            "receipt": str(output),
                        }
                    ),
                    flush=True,
                )
                return 0
            time.sleep(2)
        raise TimeoutError("Four-stage control exceeded qualification budget")
    except Exception as error:
        receipt.update(
            status="failed",
            error=str(error),
            seconds=round(time.monotonic() - started, 3),
        )
        atomic(
            args.output or Settings.load().data / "qualification-failure.json", receipt
        )
        print(
            json.dumps({"status": "failed", "error": str(error)}),
            file=sys.stderr,
            flush=True,
        )
        return 1
    finally:
        if supervisor:
            supervisor.send_signal(signal.SIGTERM)
            try:
                supervisor.wait(timeout=60)
            except subprocess.TimeoutExpired:
                supervisor.kill()
                supervisor.wait()


if __name__ == "__main__":
    raise SystemExit(main())
