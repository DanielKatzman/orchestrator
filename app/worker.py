from __future__ import annotations

import ctypes
import json
import os
import queue
import signal
import subprocess
import threading
import time
import traceback

from .common import atomic, now, read, sha
from .config import ROOT, Settings
from .processes import identity, signal_group
from .store import Store


class Cancelled(Exception):
    pass


class NeedsReview(Exception):
    pass


def parent_death_signal():
    if os.name == "posix":
        parent = os.getppid()
        ctypes.CDLL(None).prctl(1, signal.SIGINT)
        if os.getppid() != parent:
            os.kill(os.getpid(), signal.SIGINT)


class Coordinator:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.stopping = threading.Event()

    def command(self, job, phase, stage, args, *, review=False):
        attempt = self.settings.attempt(job)
        logs = attempt / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        self.store.update(job["id"], phase=phase, detail=None)
        if self.stopping.is_set() or self.store.get(job["id"])["cancelled"]:
            raise Cancelled()
        command = [self.settings.python(stage), *map(str, args)]
        start = time.monotonic()
        self.store.event(job["id"], "phase", {"phase": phase, "state": "running"})
        kwargs = (
            {"start_new_session": True, "preexec_fn": parent_death_signal}
            if os.name == "posix"
            else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        )
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=self.settings.environment(stage),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
        record = {
            **identity(process.pid),
            "phase": phase,
            "command": command,
            "started": now(),
        }
        atomic(logs / f"{phase}.process.json", record)
        messages = queue.Queue(maxsize=2000)

        def pump(stream, name):
            try:
                for line in stream:
                    messages.put((name, line))
            finally:
                stream.close()
                messages.put((name, None))

        for stream, name in [(process.stdout, "stdout"), (process.stderr, "stderr")]:
            threading.Thread(target=pump, args=(stream, name), daemon=True).start()
        finished = 0
        output = []
        output_bytes = 0
        cancelled_at = None
        last_heartbeat = 0
        last_cancel_check = 0
        last_log_event = 0
        cancel_requested = False
        with (logs / f"{phase}.log").open("a", encoding="utf-8", buffering=1) as log:
            log.write(f"\n[{now()}] Starting {phase}\n")
            while process.poll() is None or finished < 2:
                current = time.monotonic()
                if current - last_cancel_check > 0.25:
                    cancel_requested = self.store.get(job["id"])["cancelled"]
                    last_cancel_check = current
                if (
                    self.stopping.is_set() or cancel_requested
                ) and cancelled_at is None:
                    cancelled_at = current
                    signal_group(process)
                if (
                    cancelled_at is not None
                    and current - cancelled_at > self.settings.cancel_grace
                ):
                    signal_group(process, force=True)
                if current - last_heartbeat > 5:
                    self.store.heartbeat()
                    self.store.event(
                        job["id"],
                        "heartbeat",
                        {"phase": phase, "seconds": round(current - start, 1)},
                    )
                    last_heartbeat = current
                try:
                    stream, line = messages.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    finished += 1
                    continue
                log.write(f"[{stream}] {line}")
                if stream == "stdout" and output_bytes < 1_000_000:
                    output.append(line)
                    output_bytes += len(line)
                # Keep complete logs, but bound SQLite progress transactions.
                if (
                    line.strip()
                    and current - last_log_event > 0.2
                    and not (phase == "stage3" and stream == "stdout")
                ):
                    self.store.event(
                        job["id"],
                        "log",
                        {"phase": phase, "stream": stream, "line": line.strip()[:2000]},
                    )
                    last_log_event = current
        code = process.wait()
        atomic(
            logs / f"{phase}.process.json",
            {**record, "finished": now(), "returncode": code},
        )
        receipt = {
            "returncode": code,
            "seconds": round(time.monotonic() - start, 3),
            "log_sha256": sha(logs / f"{phase}.log"),
        }
        self.store.receipt(
            job,
            phase,
            "ready"
            if code == 0
            else "review_required"
            if code == 3 and review
            else "failed",
            receipt,
        )
        self.store.event(job["id"], "phase", {"phase": phase, **receipt})
        if cancelled_at is not None:
            raise Cancelled()
        if code == 3 and review:
            raise NeedsReview("Review the pending masks before continuing")
        if code != 0:
            raise RuntimeError(f"{phase} exited {code}; inspect its stage log")
        return "".join(output)

    def run(self, job):
        settings = self.settings
        root, attempt = settings.job(job["id"]), settings.attempt(job)
        attempt.mkdir(parents=True, exist_ok=True)
        io = ROOT / "tools/stage_io.py"
        try:
            request = read(root / "request.json")
            if (
                request != job["request"]
                or sha(root / "pipeline.lock.json") != settings.lock_hash
            ):
                raise ValueError(
                    "Job request or installed composition changed; use its original image"
                )
            if request.get("application_sha256") != settings.application_hash:
                raise ValueError("Job application code changed; use its original image")
            if sha(root / "capture_profile.json") != request["capture_profile_sha256"]:
                raise ValueError("Acquisition declaration changed")
            for item in request["files"]:
                if sha(root / "inputs/original" / item["name"]) != item["sha256"]:
                    raise ValueError("Uploaded capture changed")
            if job["attempt"] == 1:
                atomic(
                    attempt / "attempt.json",
                    {"kind": "raw", "lock_sha256": settings.lock_hash},
                )
            self.command(
                job,
                "inventory",
                "stage1",
                [
                    io,
                    "normalize",
                    "--input",
                    root / "inputs/original",
                    "--output",
                    root / "inputs/normalized",
                    "--dpi",
                    request["pdf_dpi"],
                    "--expected",
                    request["expected_pieces"],
                ],
            )
            inventory = root / "inputs/inventory.json"
            metadata = read(attempt / "attempt.json")
            if metadata["kind"] == "assembly":
                evidence = root / "attempts" / f"{metadata['evidence_attempt']:04d}"
                s1, s3, s4 = (
                    evidence / "stage1",
                    evidence / "stage3",
                    attempt / "stage4",
                )
                if (
                    sha(s3 / "bundle/bundle.json") != metadata["bundle_sha256"]
                    or sha(s1 / "capture_profile.json") != metadata["capture_sha256"]
                ):
                    raise ValueError("Reused evidence or capture profile changed")
                for phase in ("stage1", "boundary", "stage2", "stage3"):
                    self.store.receipt(
                        job,
                        phase,
                        "reused",
                        {"evidence_attempt": metadata["evidence_attempt"]},
                    )
            else:
                if metadata["kind"] == "review":
                    raw = (
                        root
                        / "attempts"
                        / f"{metadata['raw_attempt']:04d}"
                        / "stage1_native"
                    )
                    effective = attempt / "stage1_reviewed"
                    self.command(
                        job,
                        "review_publication",
                        "stage1",
                        [
                            io,
                            "review",
                            "--input",
                            raw,
                            "--decisions",
                            attempt / "review/decisions.json",
                            "--output",
                            effective,
                        ],
                    )
                else:
                    raw = effective = attempt / "stage1_native"
                    command = [
                        "-m",
                        "universal_segmenter.release_cli",
                        "run",
                        "--input",
                        root / "inputs/normalized",
                        "--output",
                        raw,
                        "--models",
                        settings.components / "stage1/models",
                        "--capture-profile",
                        root / "capture_profile.json",
                        "--review",
                        request["review"],
                        "--cpu-threads",
                        request["cpu_threads"],
                        "--pdf-dpi",
                        request["pdf_dpi"],
                    ]
                    if raw.exists():
                        command.append("--resume")
                    self.command(job, "stage1", "stage1", command, review=True)
                self.command(
                    job,
                    "acceptance",
                    "stage1",
                    [io, "gate", "--input", effective, "--inventory", inventory],
                    review=True,
                )
                s1, s2, s3, s4 = [
                    attempt / name for name in ("stage1", "stage2", "stage3", "stage4")
                ]
                self.command(
                    job,
                    "boundary",
                    "stage1",
                    [io, "boundary", "--input", effective, "--output", s1],
                    review=True,
                )
                command = [
                    "-m",
                    "paper_reconstruction_stage2.release_cli",
                    "run",
                    "--input",
                    s1,
                    "--output",
                    s2,
                    "--models",
                    settings.components / "stage2/models",
                    "--cpu-threads",
                    request["cpu_threads"],
                ]
                if s2.exists():
                    command.append("--resume")
                manifest = s2 / "edge_hypotheses_manifest.json"
                if manifest.exists() and read(manifest).get("status") == "complete":
                    # Re-running even a completed Stage 2 run can republish its
                    # metadata. Stage 3 authenticates input file identities,
                    # including mtimes, so completed upstream output must only
                    # be independently inspected during downstream recovery.
                    command = [
                        "-m",
                        "paper_reconstruction_stage2.release_cli",
                        "inspect",
                        "--input",
                        s2,
                        "--models",
                        settings.components / "stage2/models",
                    ]
                self.command(job, "stage2", "stage2", command)
                if (s3 / "bundle/bundle.json").exists():
                    self.store.receipt(
                        job,
                        "stage3",
                        "reused",
                        {"bundle_sha256": sha(s3 / "bundle/bundle.json")},
                    )
                else:
                    self.command(
                        job,
                        "stage3",
                        "stage3",
                        [
                            "-m",
                            "paper_reconstruction_stage3_release",
                            "prepare",
                            "--stage1",
                            s1,
                            "--stage2",
                            s2,
                            "--models",
                            settings.components / "stage3/models",
                            "--output",
                            s3,
                            "--puzzle-id",
                            job["id"],
                            "--device",
                            "cpu",
                        ],
                    )
            self.command(
                job,
                "bundle_validation",
                "stage3",
                [
                    "-m",
                    "paper_reconstruction_stage3_release",
                    "inspect",
                    "--bundle",
                    s3 / "bundle",
                ],
            )
            configs = settings.components / "stage4/stages/stage4/configs"
            config, profile = (
                configs / "joint_v21_cut_repair_development.json",
                configs / "shared_scale_complete_rectangle_predicted_v1.json",
            )
            command = [
                "-m",
                "paper_reconstruction_stage4",
                "solve",
                "--bundle",
                s3 / "bundle",
                "--config",
                config,
                "--constraint-profile",
                profile,
                "--capture-profile",
                s1 / "capture_profile.json",
                "--output",
                s4,
                "--progress",
            ]
            if s4.exists() or s4.with_name("stage4.work").exists():
                command.append("--resume")
            self.command(job, "stage4", "stage4", command)
            summary = json.loads(
                self.command(
                    job,
                    "assessment_validation",
                    "stage4",
                    [
                        io,
                        "verify4",
                        "--input",
                        s4,
                        "--bundle",
                        s3 / "bundle",
                        "--capture",
                        s1 / "capture_profile.json",
                        "--config",
                        config,
                        "--profile",
                        profile,
                        "--inventory",
                        inventory,
                    ],
                )
            )
            atomic(attempt / "summary.json", summary)
            if self.store.get(job["id"])["cancelled"]:
                raise Cancelled()
            self.store.finish(
                job["id"],
                state="completed",
                phase="finished",
                outcome=summary["outcome"],
                detail=None,
            )
        except NeedsReview as error:
            self.store.finish(job["id"], state="review_required", detail=str(error))
        except Cancelled:
            cancelled = self.store.get(job["id"])["cancelled"]
            self.store.finish(
                job["id"],
                state="cancelled" if cancelled else "interrupted",
                detail="Committed stage work was retained",
            )
        except Exception as error:
            atomic(
                attempt / "failure.json",
                {"error": str(error), "traceback": traceback.format_exc(), "at": now()},
            )
            self.store.finish(job["id"], state="failed", detail=str(error))

    def serve(self):
        for job in self.store.list():
            if job["state"] in {"running", "cancelling"}:
                self.store.finish(
                    job["id"],
                    state="interrupted",
                    detail="Worker restarted; verify and resume committed work",
                )
        while not self.stopping.is_set():
            self.store.heartbeat()
            job = self.store.claim()
            if job:
                self.run(job)
            else:
                self.stopping.wait(0.5)


def main():
    settings = Settings.load()
    store = Store(settings.data)
    lock = (settings.data / "worker.lock").open("a+")
    if os.name == "posix":
        import fcntl

        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    else:
        import msvcrt

        lock.write("0")
        lock.flush()
        lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
    worker = Coordinator(settings, store)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: worker.stopping.set())
    worker.serve()


if __name__ == "__main__":
    main()
