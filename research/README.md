# Source investigation

The main recommendation is in `../ORCHESTRATOR_AND_GUI_PLAN.md`.

`REMOTE_AUDIT.json` records live GitHub release/PR metadata and Git refs checked on 28 October 2026. `SOURCE_AUDIT.json` records the inspected commit identities, README hashes, available source-inventory checks and shared SDK comparison.

Run `python -B research/audit_sources.py` from the workspace root to repeat the source-only audit while the research checkouts are present. The four checkouts under `sources/` are ignored research material, not application dependencies or vendored stage releases. They are pinned to the commits recorded in the audit; Stage 1/2 ONNX files and Stage 4's archived weights remain unhydrated Git LFS pointers. Standalone Stage 3 includes its actual small rankers.

This directory preserves the original planning investigation. The implementation, qualified Docker image and GUI now exist; see `../README.md` and `../docs/VALIDATION.md` for application behavior and fresh container test evidence. Runtime evidence cited by the original plan came from the supplied handoff receipts and upstream release documentation.
