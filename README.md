# Paperwork — paper reconstruction orchestrator

One local browser app and one CPU Docker container for the four-stage paper reconstruction pipeline. The image includes the selected stage packages, interpreters, frozen model assets, contract SDK, configuration and compiled UI. Jobs do not download code or models.

## Run the published image

The self-contained Linux amd64 image is published as `ghcr.io/danielkatzman/orchestrator:0.1.0`. It includes all four stages, frozen models, Python runtimes, dependencies and the compiled GUI. Teammates need Docker with Linux containers and a browser; there are no separate model or source installations. Processing works offline after the image has been downloaded.

For the quickest start, download and extract the [starter ZIP from the v0.1.0 release](https://github.com/DanielKatzman/orchestrator/releases/tag/v0.1.0). It includes `compose.yaml`, startup instructions and four generated sample captures. Run `docker compose pull` and `docker compose up -d` from the extracted folder.

Download [compose.release.yaml](https://github.com/DanielKatzman/orchestrator/blob/main/compose.release.yaml) into an empty folder, then run:

```powershell
docker compose -f compose.release.yaml pull
docker compose -f compose.release.yaml up -d
```

Open **[http://localhost:8000](http://localhost:8000)**. Each computer has its own local instance and persistent `paper-orchestrator-data` volume. Stop it with `docker compose -f compose.release.yaml stop`. Keep the volume to retain jobs and results.

The image currently targets Intel/AMD 64-bit machines; native ARM/Apple Silicon and CUDA are not qualified. The CPU application defaults to four computation threads and one job at a time. There is no container RAM cap in either Compose file; Docker Desktop/WSL determines the available host resources. The Docker image's storage size is separate from its RAM usage.

## Build from source

Install/start Docker Desktop with its Linux engine, then run from this directory:

```powershell
.\scripts\build.ps1 -LatestCompatible
docker compose up -d
```

Open **[http://localhost:8000](http://localhost:8000)**. The build can take several minutes: it downloads the runtime/model assets on the first build, then runs two real four-stage controls before promoting `paper-orchestrator:local`.

On Linux:

```bash
bash scripts/build.sh --latest-compatible
docker compose up -d
```

The build scripts require Docker only on the host. Python, Node, Git LFS and a GPU are not host requirements. The initial target is Linux amd64. Windows Docker Desktop is tested; native ARM/macOS and CUDA images are separate qualification work.

If the local image has already been built and qualified, only `docker compose up -d` is needed. `docker compose stop` stops the application while retaining its data. `docker compose logs --tail 100` shows startup/health errors. Port 8000 binds to localhost; set `PAPER_PORT` before running Compose to choose another host port.

## Use

1. Create a reconstruction, name it, and upload the captures. PNG, JPEG, single-frame TIFF/BMP and PDFs are accepted. Each image or PDF page represents one physical piece.
2. Confirm that these are all pieces from one complete rectangular page, at a common native pixel scale, without independent resizing or perspective distortion. The declaration does not calibrate a camera. PDF pages are rendered at a fixed 300 DPI for the job.
3. Follow the stage timeline. You can close/refresh the browser; the worker continues independently. The application shows observed stage activity rather than an invented overall percentage or ETA.
4. If review is required, use the native-resolution brush editor, approve every pending piece, then continue. Corrections create a new immutable attempt. Raw predictions and review decisions are retained.
5. Inspect the assembly and independent assessment. Download individual artifacts or export the complete job ZIP, including inputs, manifests, logs, result and adjacent assessment package.

`supported` requires complete physical inventory, connected output, valid publication/profile bindings, independent native support and physical checks, and a supported assessment. It is not a calibrated correctness percentage or proof of pixel perfection. Partial/unresolved and runtime failures remain distinct.

The standard page route requires every physical piece. A rejected, duplicate, empty, disconnected or clipped piece is not silently omitted. Arbitrary perspective, individual resizing, mixed scales and missing-piece reconstruction are outside this release's qualified acquisition profile.

## Stages and updates

| Stage | Initial selected source | Runtime |
| --- | --- | --- |
| Segmentation | `a0e0fae627f16910065471cf1e829bcb1e2a9685`, v2 integration PR | Python 3.14, CPU ONNX |
| Boundary observations | `8b095867f1eea7216920196fd36c08a4f57b2820`, v2 integration PR | Same isolated Stage 1/2 environment |
| Pair placements | v2.0.0, `38bf18a42d0e1f74470f60f137720ae15a6b6e5d` | Python 3.12, CPU Torch 2.7.1 |
| Global assembly | v2.0.5, `919f206772984798a317b40d53443ded6529ceff` | Separate Python 3.12 NumPy/SciPy environment |

Stage 1/2 v2 are still draft integration PRs at the initial release. Their exact commits are explicitly pinned in [composition/pipeline.yaml](composition/pipeline.yaml). Stage 3/4 use stable release discovery. When Stage 1/2 publish compatible stable v2 releases, change their selectors to `channel: stable` to enable stable discovery. Do not independently clone floating default branches for jobs.

The resolver checks actual schemas, authenticated Stage 2 model/configuration identity, Stage 3 implementation and Stage 4/SDK compatibility. It keeps the existing qualified tuple if a newer source is incompatible. Schema/SDK changes that require new adapters are rejected. Resolution details are written to `.build/resolution.json`; the installed exact composition is [composition/pipeline.lock.json](composition/pipeline.lock.json).

To choose an explicit version:

```powershell
.\scripts\build.ps1 -Stage stage3=v2.0.0
```

For a new Stage 4 implementation that has not yet been qualified with the current Stage 3 release:

```powershell
.\scripts\build.ps1 -Stage stage4=v2.0.5 -Candidate
```

`-Candidate` permits qualifying a new solver fingerprint. It does **not** bypass model, SDK, schema or Stage 3 implementation checks. The same offline integration controls must pass before promotion. The smoke controls establish integration behavior on their generated four-piece page; a teammate changing algorithm quality still owns appropriate upstream regressions and real-input qualification.

Builds use fixed base-image digests, exact stage commits and model hashes, `web/package-lock.json`, the app requirements, upstream constraints and the tested Linux pins in `composition/runtime/`. Installation reports record downloaded artifact hashes inside `/opt/build`; `runtime.json` records the actual interpreters and installed package versions. The external build receipt records the image digest. Ordinary stage releases do not need a GUI/API change; a genuine dependency or wire-contract change needs an explicit runtime/adapter update.

Build scripts retain the previous source lock, candidate image, qualification receipts and control volumes. They do not restart your running app automatically. Stop active work cleanly before switching images, then run `docker compose up -d`. Old incomplete jobs require their original composition **and application/runtime identity** to resume. Keep old image digests for this purpose. A future database schema change also requires a compatible migration/backup strategy.

To distribute a built image without a registry:

```powershell
docker save paper-orchestrator:local -o paper-orchestrator.tar
# On the receiving machine:
docker load -i paper-orchestrator.tar
docker compose up -d
```

## Jobs, storage and recovery

`paper-orchestrator-data` is a persistent Docker volume mounted at `/data`. It contains SQLite state and job-owned inputs, logs, attempts and stage publications. Keep the live database/caches in this local Linux volume rather than a OneDrive or network-share directory. Export job ZIPs wherever you want afterward. For a full volume backup, stop the app and archive the volume consistently; copying a live SQLite file alone is not a backup procedure.

The worker claims one job at a time. Cancellation interrupts the stage process group, allows cooperative cleanup, then escalates after a grace period. Committed artifacts remain available. `Verify & resume` checks the original request, image identities and applicable stage publications/caches before reusing work. It does not promise exact Stage 4 search-cursor continuation.

Stage 2 uses an OS advisory lock, which the OS releases on process exit; its lock file is intentionally retained. Stage 1/3/4 PID-marker recovery happens only on explicit Resume and only for authenticated, stopped process owners. Unknown/live owners require inspection. No expired heartbeat authorizes takeover.

`Run assembly again` creates a fresh Stage 4 attempt using the verified immutable Stage 3 evidence and original bound capture profile. It does not rerun segmentation, Stage 2 or expensive Stage 3 inference, and supplies no reference poses or previous successful assembly as a seed. Stage 4 recovery requests remain diagnostic downloads; automatic Stage 3 recovery loops are not implemented.

The application owns `native-mask-review.v1`. It publishes a new reviewed Stage 1 handoff using the consumer's canonical `segformer_v3_reviewed` profile before Stage 2/3 bind it, preserving raw output. This avoids changing the frozen upstream sources or relabelling an already-bound profile. The full reviewed route is separately exercised by the image control.

## Development and tests

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt -e ".[test]"
.\.venv\Scripts\python.exe -m pytest -q
cd web
npm ci
npm run build
```

For a development server, supply `PAPER_COMPONENTS` and a `PAPER_RUNTIMES` JSON mapping containing the four explicit interpreter paths, then run `python -m app.supervisor`. Stage dependencies and models must already be installed. Production subprocesses use installed packages and absolute venv launchers; venv symlinks are never dereferenced.

The build command runs controls inside the final image with `--network none` and a read-only application filesystem:

- The automatic route cancels during real Stage 3 execution, resumes authenticated work, and requires a supported connected four-piece assembly.
- The review-all route stops at review, submits a one-native-pixel correction, publishes a new attempt and requires the same complete supported outcome.

Failed controls retain their containers/data and do not promote the image or lock. Broader upstream development/real-capture benchmarks are distinct from these interoperability controls. See [docs/VALIDATION.md](docs/VALIDATION.md) for the actual results of this implementation.

The API is documented at [http://localhost:8000/docs](http://localhost:8000/docs). Its job, review, event, artifact and assembly-attempt endpoints use `/api/v1`. `/healthz` is process liveness; `/readyz` additionally checks the coordinator and installed assets.

The server currently targets a local workstation. A shared remote service needs authenticated access/job ownership before changing the default localhost deployment.

## Source organization

- `app/`: API, SQLite store, coordinator, process supervision and real-image qualification command.
- `tools/`: version resolution, source/model fetching, stage file/review/assessment adapters and runtime identity recording.
- `composition/`: source lock, selection policy, runtime pins, native-frame adapter and capture declaration.
- `web/`: React/TypeScript UI and browser mask editor.
- `tests/`: API/lifecycle/compatibility checks and the generated four-capture control.
- `docker/`, `scripts/`, `compose.yaml`: container construction, qualification, promotion and startup.

The original research and full design are retained in [ORCHESTRATOR_AND_GUI_PLAN.md](ORCHESTRATOR_AND_GUI_PLAN.md) and `research/`. Research checkouts, downloaded assets, local environments, databases, logs and build outputs are ignored by Git. The source repository is [DanielKatzman/orchestrator](https://github.com/DanielKatzman/orchestrator). Upstream Stage 2/4 copyright notices and Stage 1 third-party notices are retained inside the image under `/opt/notices`.

## Publish a release

The [release workflow](.github/workflows/publish.yaml) builds the committed composition on Linux amd64 and runs both real offline controls before pushing the versioned image and `latest` to GitHub Container Registry. It uses the repository's short-lived `GITHUB_TOKEN`; no personal registry token is stored in the repository. The image source label associates the package with this repository.

Update the version in `pyproject.toml` and `compose.release.yaml`, commit the change, then push its matching tag (for example `v0.1.0`). The workflow can also be run manually. Qualification receipts and the registry digest are retained as workflow artifacts. Set the container package visibility to public for anonymous teammate downloads; GitHub makes a new package private initially even when its source repository is public. Publish only after both controls pass; a Git tag alone does not imply the image has been published.
