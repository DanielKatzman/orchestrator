# Implementation validation — 28 October 2026

The local implementation was tested on Windows Docker Desktop's Linux amd64 engine. The final selected sources are the exact four commits in `composition/pipeline.lock.json`; Stage 1/2 remain explicit v2 integration-PR pins. No upstream code was modified, no model was trained, and no GitHub repository was created or published.

## Final image controls

The documented build/promotion workflow was exercised. Live latest-compatible resolution selected the initial tuple. The pinned final build completed both controls before promoting `paper-orchestrator:local`.

Docker image identity: `sha256:31787342953b3968d8b26453b40c26e613c7433f5aace95214de2c7830e3647b`. Docker-reported image size: 667,156,131 bytes. Application/runtime identity: `63324c34f3734e5dac7231fd796b29e14b4d2bed059e10a4c1b0f1307c5988b7`. Composition lock: `789ec2a279499d09a21ac4b560ba6b5d358bd31ddb541d81d1b177a71d5f5c83`.

Both controls used the final image with outbound networking disabled (`--network none`), a read-only application filesystem, writable temporary space and an independent persistent data volume. They exercised the actual HTTP API, SQLite coordinator and all four real CPU stage packages/models; no fake stage implementations were substituted.

| Control | Result | Pieces / selected joins | End-to-end time |
| --- | --- | --- | --- |
| Automatic route, deliberate Stage 3 cancellation and authenticated resume | passed; independent assessment supported and all pieces connected | 4 / 3 | 125.374 s |
| Review-all route, actual one-native-pixel mask correction and new downstream attempt | passed; independent assessment supported and all pieces connected | 4 / 3 | 106.046 s |

Automatic job: `0b4d5544ce1f4fdba362d1d1d362d37c`. Reviewed job: `edf44696f12640e99f157615c56b7044`. Detailed stage receipts, artifact hashes, runtime versions and source/model identities are in [automatic-cancellation.json](validation/automatic-cancellation.json) and [reviewed-mask.json](validation/reviewed-mask.json). Image metadata is in [image.json](validation/image.json). Times describe this generated four-piece control on this workstation, including workflow overhead; they are not general throughput promises.

The automatic control exposed an initial recovery bug: re-running an already complete Stage 2 publication changed metadata used by Stage 3's resume identity. The coordinator now independently inspects completed Stage 2 output instead of republishing it, and fully validates/reuses an already published Stage 3 bundle. The corrected cancellation/resume control passes. A separate atomic finalization guard ensures that a cancellation accepted during publication cannot be overwritten by a completed/supported execution state.

## Application and browser verification

- 20 API, lifecycle, input, compatibility and concurrency tests passed. The suite includes duplicate/corrupt inputs, required acquisition declaration, idempotency conflicts, containment, exclusive claims, subprocess interruption, advisory/PID-marker lock distinctions, verbose-output bounds and both orderings of cancellation versus final publication.
- Python lint checks passed, and the TypeScript/production Vite build passed.
- The browser upload/review flow was exercised with all four generated captures. A disconnected correction was rejected. Undo followed by save succeeded; decoded saved mask pixels exactly matched the original at native size 660 × 820.
- Wide and narrow editor layouts were inspected. Canvas buffers matched their display sizes (739 × 510 and 610 × 400), while the submitted mask retained its original native dimensions.
- An assembly-only rerun reached a supported connected four-piece result with Stage 1/2/3 explicitly marked reused.
- A complete job ZIP was downloaded through the API, checked for archive integrity, and found to contain both result and adjacent assessment manifests. The checked export contained 230 files.
- A service restart preserved the completed result byte-for-byte and retained the pending review state and saved mask draft. The editor-only test job was then cancelled without deleting its history.

The browser/restart/assembly evidence is in [live-application.json](validation/live-application.json); the final running-image check is recorded separately in [running-application.json](validation/running-application.json). The compiled UI was unchanged by the final atomic state-transition guard. The final server is bound to `http://localhost:8000` and uses the persistent `paper-orchestrator-data` volume. Older demonstration jobs were preserved when the final qualified control was added.

## Scope

The final result screen is captured in [reconstruction.jpg](screenshots/reconstruction.jpg).

These controls establish container packaging, stage interoperability, native-mask review, durable coordination, cancellation/recovery and user-interface behavior on the bundled generated page. They do not establish unseen real-photo accuracy, calibrated confidence, 30-piece CPU throughput, native ARM/macOS behavior or GPU support. Broader development/real-input qualification remains distinct. Upstream historical benchmark results were not relabelled as tests of this application.

## Published release — 29 October 2026

Source commit `46fcd6e18340d64a9ec96a2cf30507454ef969dc` is tagged `v0.1.0` in [DanielKatzman/orchestrator](https://github.com/DanielKatzman/orchestrator). The [GitHub release workflow](https://github.com/DanielKatzman/orchestrator/actions/runs/40887781730) passed all 20 tests, built the committed composition on Linux amd64, and passed both real offline controls before publishing `ghcr.io/danielkatzman/orchestrator:0.1.0` and `:latest`.

The registry digest is `sha256:ec6182618418c4e962647802d65294ee29f3a24e3464d748bf2c48d92c99159b`; both tags were independently checked to match. The automatic cancellation/resume control took 169.451 seconds and the corrected-mask control took 148.959 seconds. Both reached supported four-piece results. A complete image pull succeeded using an empty Docker credential configuration, verifying anonymous teammate downloads.

This GitHub-built image is separately qualified from the workstation-built image above; its application/runtime identity is `6b363512db1441a157da6f439ff5674e6159d311a8692c3d7c7b1a6e5645da37`. The current local application and its persistent jobs were left running on their original image. The hosted release summary is [github-release.json](validation/github-release.json); full receipts and image metadata are retained in the workflow qualification artifact.
