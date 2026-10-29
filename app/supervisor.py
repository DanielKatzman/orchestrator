"""Own both application processes and propagate container shutdown."""

import signal
import subprocess
import sys
import time


def main():
    processes = [
        subprocess.Popen([sys.executable, "-m", "app.worker"]),
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.api:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
                "--no-access-log",
            ]
        ),
    ]
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping and all(p.poll() is None for p in processes):
        time.sleep(0.3)
    abnormal = not stopping
    for process in processes:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
    deadline = time.monotonic() + 55
    for process in processes:
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    return 1 if abnormal else 0


if __name__ == "__main__":
    raise SystemExit(main())
