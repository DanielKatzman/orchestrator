import React, { useEffect, useRef, useState } from "react";

export type Piece = {
  capture_id: string;
  source_capture_id: string;
  accepted: boolean;
  approved: boolean;
  decision: string;
  empty_proposal: boolean;
  origin: string;
  base_sha256: string;
  image: string;
  mask: string;
  edited_mask?: string;
  overlay: string;
  piece?: string;
  original_size_hw: number[];
  bbox_xywh: number[];
  reason_codes: string[];
};
type Source = {
  source_capture_id: string;
  name: string;
  image: string;
  original_size_hw: number[];
  confirmed: boolean;
  empty_reviewed: boolean;
  piece_count: number;
  approved_count: number;
  proposal_count: number;
};
type Snapshot = {
  revision: string;
  sources: Source[];
  pieces: Piece[];
  selected_piece_id?: string;
};
type EditorProps = {
  piece: Piece;
  jobId: string;
  onSaved: () => void;
  onError: (message: string) => void;
  onDirty?: (dirty: boolean) => void;
};
const active = (piece: Piece) =>
  !piece.empty_proposal && !["rejected", "superseded"].includes(piece.decision);

function SourceOverview({
  source,
  pieces,
  selected,
  onSelect,
  onError,
}: {
  source: Source;
  pieces: Piece[];
  selected: string;
  onSelect: (id: string) => void;
  onError: (s: string) => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const hit = useRef<{
    labels: Int16Array;
    ids: string[];
    width: number;
  } | null>(null);
  const [loading, setLoading] = useState(true);
  const signature = pieces
    .map((p) => `${p.capture_id}:${p.base_sha256}:${p.decision}`)
    .join("|");
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    hit.current = null;
    const load = (url: string) =>
      new Promise<HTMLImageElement>((resolve, reject) => {
        const image = new Image();
        image.onload = () => resolve(image);
        image.onerror = reject;
        image.src = url;
      });
    (async () => {
      const image = await load(source.image);
      if (cancelled || !canvas.current) return;
      const c = canvas.current;
      const scale = Math.min(900 / image.width, 520 / image.height, 1);
      c.width = Math.max(1, Math.round(image.width * scale));
      c.height = Math.max(1, Math.round(image.height * scale));
      const context = c.getContext("2d")!;
      context.drawImage(image, 0, 0, c.width, c.height);
      const labels = new Int16Array(c.width * c.height);
      const layer = document.createElement("canvas");
      layer.width = c.width;
      layer.height = c.height;
      const ctx = layer.getContext("2d", { willReadFrequently: true })!;
      const overlay = document.createElement("canvas");
      overlay.width = c.width;
      overlay.height = c.height;
      for (let number = 0; number < pieces.length; number++) {
        const piece = pieces[number];
        const mask = await load(piece.edited_mask || piece.mask);
        if (cancelled) return;
        ctx.clearRect(0, 0, c.width, c.height);
        ctx.imageSmoothingEnabled = false;
        ctx.drawImage(mask, 0, 0, c.width, c.height);
        const pixels = ctx.getImageData(0, 0, c.width, c.height);
        const output = ctx.createImageData(c.width, c.height);
        const color =
          piece.capture_id === selected
            ? [44, 121, 230]
            : piece.approved
              ? [29, 142, 91]
              : [218, 145, 22];
        for (let i = 0; i < labels.length; i++) {
          if (pixels.data[i * 4] <= 127) continue;
          labels[i] = number + 1;
          const edge =
            i % c.width === 0 ||
            i % c.width === c.width - 1 ||
            i < c.width ||
            i >= labels.length - c.width ||
            [i - 1, i + 1, i - c.width, i + c.width].some(
              (j) => pixels.data[j * 4] <= 127,
            );
          output.data.set([...color, edge ? 255 : 65], i * 4);
        }
        overlay.getContext("2d")!.putImageData(output, 0, 0);
        context.drawImage(overlay, 0, 0);
      }
      if (!cancelled) {
        hit.current = {
          labels,
          ids: pieces.map((p) => p.capture_id),
          width: c.width,
        };
        setLoading(false);
      }
    })().catch(
      () => !cancelled && onError("Could not load the photo overview."),
    );
    return () => {
      cancelled = true;
    };
  }, [source.source_capture_id, signature, selected]);
  return (
    <div className="source-overview">
      <canvas
        ref={canvas}
        aria-label="Detected pieces in source photo"
        onClick={(event) => {
          const c = event.currentTarget,
            bounds = c.getBoundingClientRect(),
            map = hit.current;
          if (!map) return;
          const x = Math.floor(
            ((event.clientX - bounds.left) * c.width) / bounds.width,
          );
          const y = Math.floor(
            ((event.clientY - bounds.top) * c.height) / bounds.height,
          );
          const label = map.labels[y * map.width + x];
          if (label) onSelect(map.ids[label - 1]);
        }}
      />
      <p>
        {loading
          ? "Loading piece outlines…"
          : pieces.length
            ? "Click a colored piece to review its outline. Amber: pending · Green: approved · Blue: selected."
            : "No retained pieces. Confirm this photo is empty, or add a missed piece."}
      </p>
    </div>
  );
}

export function ReviewPanel({
  job,
  api,
  Editor,
  onChanged,
  onError,
}: {
  job: { id: string; attempt: number; request: any };
  api: (path: string, init?: RequestInit) => Promise<any>;
  Editor: React.ComponentType<EditorProps>;
  onChanged: () => Promise<void>;
  onError: (message: string) => void;
}) {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [sourceId, setSourceId] = useState("");
  const [pieceId, setPieceId] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [confirmedPage, setConfirmedPage] = useState(false);
  const [expected, setExpected] = useState(
    job.request.expected_pieces ? String(job.request.expected_pieces) : "",
  );
  const apply = (next: Snapshot) => {
    setSnapshot(next);
    setSourceId((old) =>
      next.sources.some((s) => s.source_capture_id === old)
        ? old
        : next.sources[0]?.source_capture_id || "",
    );
    setPieceId(
      (old) =>
        next.selected_piece_id ||
        (next.pieces.some((p) => p.capture_id === old && active(p)) ? old : ""),
    );
    setSelected((old) =>
      old.filter((id) =>
        next.pieces.some((p) => p.capture_id === id && active(p)),
      ),
    );
  };
  const load = async () => {
    apply(await api(`/jobs/${job.id}/review`));
  };
  useEffect(() => {
    load().catch((e: Error) => onError(e.message));
  }, [job.id, job.attempt]);
  const mutate = async (path: string, data: any = {}) => {
    setBusy(true);
    onError("");
    try {
      apply(
        await api(`/jobs/${job.id}/${path}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ revision: snapshot?.revision, ...data }),
        }),
      );
      await onChanged();
    } catch (e: any) {
      onError(e.message);
      await load().catch(() => {});
    } finally {
      setBusy(false);
    }
  };
  const finish = async (reconstruct: boolean) => {
    setBusy(true);
    onError("");
    try {
      await api(`/jobs/${job.id}/review-revisions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          revision: snapshot?.revision,
          reconstruct,
          acquisition_confirmed: confirmedPage,
          expected_pieces: Number(expected || 0),
        }),
      });
      await onChanged();
    } catch (e: any) {
      onError(e.message);
    } finally {
      setBusy(false);
    }
  };
  if (!snapshot)
    return <div className="notice">Loading photo and piece review…</div>;
  const source = snapshot.sources.find((s) => s.source_capture_id === sourceId);
  const pieces = snapshot.pieces.filter(
    (p) =>
      p.source_capture_id === sourceId &&
      !p.empty_proposal &&
      p.decision !== "superseded",
  );
  const retained = pieces.filter(active);
  const current = retained.find((p) => p.capture_id === pieceId);
  const total = snapshot.pieces.filter(active).length;
  const allConfirmed =
    snapshot.sources.length > 0 && snapshot.sources.every((s) => s.confirmed);
  return (
    <section className="instance-review">
      <div className="section-top">
        <div>
          <h2>Review photos and pieces</h2>
          <p>
            {snapshot.sources.length} source photos · {total} retained pieces.
            Select a photo, then a piece to check its outline.
          </p>
        </div>
      </div>
      <div className="source-review-layout">
        <nav className="source-list" aria-label="Source photos">
          {snapshot.sources.map((s, i) => (
            <button
              key={s.source_capture_id}
              className={sourceId === s.source_capture_id ? "active" : ""}
              disabled={busy || dirty}
              onClick={() => {
                setSourceId(s.source_capture_id);
                setPieceId("");
                setSelected([]);
              }}
            >
              <img src={s.image} alt={`Source photo ${i + 1}`} />
              <span>
                Photo {i + 1}
                <small>
                  {s.piece_count} pieces ·{" "}
                  {s.confirmed
                    ? s.empty_reviewed
                      ? "Confirmed empty"
                      : "Confirmed"
                    : `${s.approved_count} approved`}
                </small>
              </span>
            </button>
          ))}
        </nav>
        {source && (
          <div className="source-workspace">
            <div className="source-title">
              <div>
                <h3>{source.name}</h3>
                <p>
                  {source.proposal_count} model proposals · {source.piece_count}{" "}
                  retained pieces
                </p>
              </div>
              <button
                disabled={busy || dirty}
                onClick={() => mutate(`sources/${sourceId}/pieces`)}
              >
                + Add missed piece
              </button>
            </div>
            <SourceOverview
              source={source}
              pieces={retained}
              selected={pieceId}
              onSelect={(id) => {
                if (!busy && !dirty) setPieceId(id);
              }}
              onError={onError}
            />
            <div
              className="piece-cards"
              aria-label="Individual piece proposals"
            >
              {pieces.map((piece, i) => (
                <div
                  key={piece.capture_id}
                  className={`piece-card ${pieceId === piece.capture_id ? "active" : ""} ${piece.decision === "rejected" ? "rejected" : ""}`}
                >
                  <input
                    type="checkbox"
                    aria-label={`Select piece ${i + 1} for merging`}
                    disabled={!active(piece) || busy}
                    checked={selected.includes(piece.capture_id)}
                    onChange={(e) =>
                      setSelected((old) =>
                        e.target.checked
                          ? [...old, piece.capture_id]
                          : old.filter((id) => id !== piece.capture_id),
                      )
                    }
                  />
                  <button
                    onClick={() => setPieceId(piece.capture_id)}
                    disabled={
                      !active(piece) ||
                      busy ||
                      (dirty && pieceId !== piece.capture_id)
                    }
                  >
                    <strong>Piece {i + 1}</strong>
                    <small>
                      {piece.decision === "rejected"
                        ? "Rejected"
                        : piece.approved
                          ? "Approved"
                          : piece.origin === "model"
                            ? "Needs review"
                            : "New outline · review needed"}
                    </small>
                  </button>
                  {piece.decision === "rejected" && (
                    <button
                      disabled={busy}
                      onClick={() =>
                        mutate(
                          `pieces/${piece.capture_id}/decision?decision=pending`,
                        )
                      }
                    >
                      Restore
                    </button>
                  )}
                </div>
              ))}
            </div>
            <div className="source-actions">
              <button
                disabled={busy || dirty || selected.length < 2}
                onClick={() => mutate("pieces/merge", { piece_ids: selected })}
              >
                Merge selected ({selected.length})
              </button>
              <button
                className={source.confirmed ? "confirmed-source" : "primary"}
                disabled={busy || dirty || retained.some((p) => !p.approved)}
                onClick={() =>
                  mutate(`sources/${sourceId}/confirm`, {
                    empty: retained.length === 0,
                  })
                }
              >
                {source.confirmed
                  ? source.empty_reviewed
                    ? "✓ Confirmed empty"
                    : "✓ Photo confirmed"
                  : retained.length
                    ? "Confirm every visible piece is labeled"
                    : "Confirm NO pieces in this photo"}
              </button>
            </div>
            {current && (
              <div className="piece-editor-section">
                <div className="section-top">
                  <div>
                    <h3>Review selected piece</h3>
                    <p>
                      Brush changes affect this piece only. Save a draft before
                      splitting separate regions.
                    </p>
                  </div>
                  <div className="piece-actions">
                    <button
                      disabled={busy || dirty}
                      onClick={() =>
                        mutate(`pieces/${current.capture_id}/split`)
                      }
                    >
                      Split regions
                    </button>
                    <button
                      disabled={busy}
                      onClick={() =>
                        mutate(
                          `pieces/${current.capture_id}/decision?decision=rejected`,
                        )
                      }
                    >
                      Reject piece
                    </button>
                  </div>
                </div>
                {dirty && (
                  <p className="unsaved-mask" role="status">
                    Unsaved outline changes. Save a draft or approve the outline
                    before continuing.
                  </p>
                )}
                <Editor
                  key={current.capture_id}
                  piece={current}
                  jobId={job.id}
                  onSaved={async () => {
                    await load();
                    await onChanged();
                  }}
                  onError={onError}
                  onDirty={setDirty}
                />
              </div>
            )}
          </div>
        )}
      </div>
      <div className="review-finish">
        <div>
          <h3>
            {allConfirmed
              ? "Every photo is confirmed"
              : "Confirm each photo before continuing"}
          </h3>
          <p>
            Save reviewed pieces on their own, or reconstruct a complete page
            from the retained pieces.
          </p>
        </div>
        <label>
          Expected physical pieces (optional)
          <input
            aria-label="Reviewed expected physical pieces"
            type="number"
            min="0"
            max="60"
            value={expected}
            placeholder="Unknown"
            onChange={(e) => setExpected(e.target.value)}
          />
        </label>
        <label className="declaration">
          <input
            type="checkbox"
            checked={confirmedPage}
            onChange={(e) => setConfirmedPage(e.target.checked)}
          />
          <span>
            These reviewed pieces form one complete rectangular page, at a
            common native pixel scale, without perspective distortion or
            independent resizing.
          </span>
        </label>
        <div className="source-actions">
          <button
            disabled={busy || dirty || !allConfirmed}
            onClick={() => finish(false)}
          >
            {total ? "Save reviewed pieces" : "Save confirmed empty photos"}
          </button>
          <button
            className="primary"
            disabled={
              busy || dirty || !allConfirmed || !total || !confirmedPage
            }
            onClick={() => finish(true)}
          >
            Reconstruct page →
          </button>
        </div>
      </div>
    </section>
  );
}
