# Stage 1 v2.5 migration

The integration follows the [v2.5 orchestrator handoff](https://github.com/DanielKatzman/paper-segmentation-stage/blob/be0393b0fe72c00bebda8c17f35ccda170b600e3/docs/ORCHESTRATOR_HANDOFF_V2_5.md) and release README. The handoff SHA-256 is `81cd68302df52401dbb74fe9326783f0d460af05ee3553c92b3bcee95eab73cb`. The model is unchanged; the stage package, proposal inventory and review workflow changed substantially.

## User workflow

Upload one or more photos/PDF pages. A photo may contain zero, one or many pieces. Select a photo in the left rail; the overview shows actual predicted masks. Click a piece or its card to open the native-size brush editor. Approve an outline or save a draft. Approval checks connectedness, native dimensions, binary pixels, clipping and overlap with other retained pieces in that photo.

Add a missed piece with a blank native mask, then paint it. Reject false positives explicitly. To split, erase the bridge between regions, save a draft, then use **Split regions**. The resulting connected components receive new observation IDs. **Merge selected** unions selected masks and creates a new pending observation; a disconnected union needs correction before approval. Superseded parents remain in review history.

Confirm each photo after approving or rejecting all its proposals. A blank photo needs **Confirm NO pieces in this photo**; model silence is not treated as approval. Adding, editing, rejecting, restoring, splitting or merging invalidates the affected photo's confirmation.

**Save reviewed pieces** finishes after segmentation with native crops and masks in the export. An entirely empty batch finishes as `no_pieces` without invoking downstream stages. Saved nonempty pieces can start reconstruction later without reuploading. **Reconstruct page** additionally requires the complete-page/common-scale acquisition declaration. Expected physical count is optional and can be corrected during review; it never constrains the number of uploaded photos.

## Identities and publications

- `source_capture_id` identifies a normalized input photo. `source_sha256` authenticates that photo.
- `capture_id` is an opaque observation ID for an individual physical-piece proposal. Several observations can share a source hash. Existing IDs remain stable through mask edits.
- `artifact_id` uniquely keys full masks, cropped masks, RGBA pieces and overlays. Materializing by source hash would overwrite other pieces from the same photo; this migration explicitly uses the unique artifact key.
- `segmentation_run.json:sources` remains independent of the effective physical manifest. Confirmed empty sources remain accounted for after placeholder rows are removed.
- The upstream verifier reports `review_required` for an empty effective manifest. The orchestrator permits the zero-piece completion only after checking complete source coverage and explicit empty confirmations; incomplete output cannot masquerade as an empty batch.
- Review revisions are locked and checksummed. Stale snapshot or mask revisions are refused. Each continuation freezes a new immutable attempt; raw model output stays unchanged. Creation lineage, rejected/superseded observations and intermediate snapshots are retained.
- The effective handoff includes only approved physical observations. Native verification checks actual mask-pixel overlap, so overlapping bounding boxes alone are valid. The reviewed capture profile is bound before Stage 2/3 consume it.
- Stage 4 output and Stage 3 evidence inventories are checked against accepted observation IDs, never against uploaded-file hashes or photo count.

The upstream manifest schema name is unchanged, but its additive fields require this application migration. The resolver accepts the implemented 2.5 multi-piece contract and refuses unknown capture modes/source inventories even with candidate qualification enabled.

## Upgrade and compatibility

Application version `0.2.0` pins Stage 1 to `be0393b0fe72c00bebda8c17f35ccda170b600e3`; Stage 2/3/4 algorithms and model weights are unchanged. All models remain inside the self-contained image. The build qualifies separate-photo, reviewed-mask, multi-photo and confirmed-empty routes offline before promoting the lock/image.

The existing SQLite schema and data volume are retained. Historical jobs remain viewable/exportable, including old review corrections. Incomplete jobs from another application/runtime/composition require their original image to resume/edit; the new worker skips them. Keep the old image and persistent volume. This migration does not reinterpret historical single-piece jobs as new multi-piece jobs.

The [v0.2.0 release](https://github.com/DanielKatzman/orchestrator/releases/tag/v0.2.0) distributes the locally qualified Docker image in a self-contained ZIP. Load its image archive, then start the included Compose file (`compose.download.yaml` in this repository). Registry publication is pending; `compose.release.yaml` prepares the future `ghcr.io/danielkatzman/orchestrator:0.2.0` route. The release workflow runs all four offline controls before registry publication. Building locally does not publish a registry image.
