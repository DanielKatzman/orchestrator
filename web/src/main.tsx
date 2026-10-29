import React, { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./style.css";

type Job = {
  id: string;
  name: string;
  state: string;
  outcome: string | null;
  phase: string | null;
  detail: string | null;
  created: string;
  updated: string;
  attempt: number;
  summary: any;
  inventory: any;
  stages: any[];
  request: any;
};
type Piece = {
  capture_id: string;
  accepted: boolean;
  approved: boolean;
  base_sha256: string;
  image: string;
  mask: string;
  edited_mask?: string;
  overlay: string;
  original_size_hw: number[];
  reason_codes: string[];
};
type Artifact = { path: string; size: number; url: string };
const phases = [
  "inventory",
  "stage1",
  "boundary",
  "stage2",
  "stage3",
  "stage4",
  "assessment_validation",
];
const labels: Record<string, string> = {
  inventory: "Prepare captures",
  stage1: "Segment pieces",
  acceptance: "Check inventory",
  boundary: "Prepare boundaries",
  stage2: "Understand edges",
  stage3: "Find pair placements",
  bundle_validation: "Verify evidence",
  stage4: "Assemble page",
  assessment_validation: "Verify assembly",
  review_publication: "Publish reviewed masks",
  finished: "Finished",
};
const readable = (s: string) => s.replaceAll("_", " ");
const date = (s: string) =>
  new Date(s).toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
async function api(path: string, init?: RequestInit) {
  const r = await fetch("/api/v1" + path, init);
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : JSON.stringify(body.detail || r.statusText),
    );
  }
  return r.json();
}
function Icon({ name, size = 20 }: { name: string; size?: number }) {
  const paths: Record<string, React.ReactNode> = {
    paper: (
      <>
        <path d="M5 3h10l4 4v14H5z" />
        <path d="M14 3v5h5M8 12h8M8 16h6" />
      </>
    ),
    plus: <path d="M12 5v14M5 12h14" />,
    grid: (
      <>
        <rect x="3" y="3" width="7" height="7" rx="1" />
        <rect x="14" y="3" width="7" height="7" rx="1" />
        <rect x="3" y="14" width="7" height="7" rx="1" />
        <rect x="14" y="14" width="7" height="7" rx="1" />
      </>
    ),
    upload: (
      <>
        <path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5" />
      </>
    ),
    arrow: <path d="M4 12h16m-6-6 6 6-6 6" />,
    check: <path d="m5 12 4 4L19 6" />,
    back: <path d="M20 12H4m6-6-6 6 6 6" />,
    layers: (
      <>
        <path d="m12 3 10 5-10 5L2 8zM2 12l10 5 10-5M2 16l10 5 10-5" />
      </>
    ),
    download: <path d="M12 3v13m-5-5 5 5 5-5M4 17v4h16v-4" />,
    close: <path d="m5 5 14 14M19 5 5 19" />,
    clock: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7v5l3 2" />
      </>
    ),
  };
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {paths[name] || paths.paper}
    </svg>
  );
}
function Badge({ job }: { job: Job }) {
  const value = job.outcome || job.state;
  return (
    <span className={"badge " + value}>
      <i />
      {readable(value)}
    </span>
  );
}

function MaskEditor({
  piece,
  jobId,
  onSaved,
  onError,
}: {
  piece: Piece;
  jobId: string;
  onSaved: () => void;
  onError: (s: string) => void;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const state = useRef<any>({});
  const [tool, setTool] = useState("add");
  const [radius, setRadius] = useState(12);
  const [saving, setSaving] = useState(false);
  const [fill, setFill] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [zoom, setZoom] = useState(1);
  const draw = useCallback(() => {
    const s = state.current;
    const c = canvas.current;
    if (!s.image || !s.mask || !c) return;
    const ctx = c.getContext("2d")!;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = "#e4e7e1";
    ctx.fillRect(0, 0, c.width, c.height);
    ctx.save();
    ctx.translate(s.x, s.y);
    ctx.scale(s.scale, s.scale);
    ctx.drawImage(s.image, 0, 0);
    if (s.fill) {
      const overlay = s.mask
        .getContext("2d")
        .getImageData(0, 0, s.mask.width, s.mask.height);
      for (let i = 0; i < overlay.data.length; i += 4) {
        const a = overlay.data[i] > 127 ? 110 : 0;
        overlay.data[i] = 41;
        overlay.data[i + 1] = 143;
        overlay.data[i + 2] = 100;
        overlay.data[i + 3] = a;
      }
      s.overlay.getContext("2d").putImageData(overlay, 0, 0);
      ctx.drawImage(s.overlay, 0, 0);
    }
    ctx.restore();
  }, []);
  const fit = () => {
    const s = state.current;
    const c = canvas.current;
    if (!c || !s.image) return;
    if (c.width !== c.clientWidth) c.width = c.clientWidth;
    if (c.height !== c.clientHeight) c.height = c.clientHeight;
    s.fit = Math.min(
      (c.width - 40) / s.image.width,
      (c.height - 40) / s.image.height,
    );
    s.scale = s.fit;
    s.x = (c.width - s.image.width * s.scale) / 2;
    s.y = (c.height - s.image.height * s.scale) / 2;
    setZoom(1);
    draw();
  };
  useEffect(() => {
    let cancelled = false;
    setLoaded(false);
    const load = (url: string) =>
      new Promise<HTMLImageElement>((resolve, reject) => {
        const i = new Image();
        i.onload = () => resolve(i);
        i.onerror = reject;
        i.src = url;
      });
    Promise.all([
      load(piece.image),
      load(piece.edited_mask || piece.mask),
      load(piece.mask),
    ])
      .then(([image, mask, original]) => {
        if (cancelled) return;
        const m = document.createElement("canvas");
        m.width = image.width;
        m.height = image.height;
        m.getContext("2d")!.drawImage(mask, 0, 0);
        const o = document.createElement("canvas");
        o.width = image.width;
        o.height = image.height;
        state.current = {
          image,
          mask: m,
          original,
          overlay: o,
          undo: [],
          fill: true,
        };
        const c = canvas.current!;
        c.width = c.clientWidth || 800;
        c.height = 510;
        fit();
        setLoaded(true);
      })
      .catch(() => onError("Could not load the native image or mask."));
    return () => {
      cancelled = true;
    };
  }, [piece.capture_id, piece.base_sha256]);
  useEffect(() => {
    state.current.fill = fill;
    draw();
  }, [fill, draw]);
  useEffect(() => {
    if (!loaded || !canvas.current) return;
    const observer = new ResizeObserver(() => fit());
    observer.observe(canvas.current);
    return () => observer.disconnect();
  }, [loaded]);
  useEffect(() => {
    const c = canvas.current;
    if (!c) return;
    const prevent = (event: WheelEvent) => event.preventDefault();
    c.addEventListener("wheel", prevent, { passive: false });
    return () => c.removeEventListener("wheel", prevent);
  }, []);
  const pushUndo = () => {
    const s = state.current;
    if (!s.mask) return;
    const bytes = s.mask.width * s.mask.height * 4;
    const limit = Math.max(
      1,
      Math.min(20, Math.floor((64 * 1024 * 1024) / bytes)),
    );
    while (s.undo.length >= limit) s.undo.shift();
    s.undo.push(
      s.mask.getContext("2d").getImageData(0, 0, s.mask.width, s.mask.height),
    );
  };
  const paint = (x: number, y: number) => {
    const s = state.current;
    const ctx = s.mask.getContext("2d");
    ctx.fillStyle = s.erase ? "black" : "white";
    const px = (x - s.x) / s.scale,
      py = (y - s.y) / s.scale;
    const prev = s.previous || [px, py];
    const n = Math.max(
      1,
      Math.ceil(
        Math.hypot(px - prev[0], py - prev[1]) / Math.max(1, radius / 3),
      ),
    );
    for (let t = 0; t <= n; t++) {
      ctx.beginPath();
      ctx.arc(
        prev[0] + ((px - prev[0]) * t) / n,
        prev[1] + ((py - prev[1]) * t) / n,
        radius,
        0,
        Math.PI * 2,
      );
      ctx.fill();
    }
    s.previous = [px, py];
    draw();
  };
  const xy = (e: React.PointerEvent) => {
    const c = e.currentTarget as HTMLCanvasElement;
    const r = c.getBoundingClientRect();
    return [
      ((e.clientX - r.left) * c.width) / r.width,
      ((e.clientY - r.top) * c.height) / r.height,
    ];
  };
  const save = async () => {
    setSaving(true);
    try {
      const s = state.current;
      const ctx = s.mask.getContext("2d");
      const data = ctx.getImageData(0, 0, s.mask.width, s.mask.height);
      for (let i = 0; i < data.data.length; i += 4) {
        const v = data.data[i] > 127 ? 255 : 0;
        data.data[i] = data.data[i + 1] = data.data[i + 2] = v;
        data.data[i + 3] = 255;
      }
      ctx.putImageData(data, 0, 0);
      const blob = await new Promise<Blob>((resolve, reject) =>
        s.mask.toBlob(
          (b: Blob | null) =>
            b ? resolve(b) : reject(new Error("Mask export failed")),
          "image/png",
        ),
      );
      const form = new FormData();
      form.set("base_sha256", piece.base_sha256);
      form.set("mask", blob, "mask.png");
      await api(`/jobs/${jobId}/pieces/${piece.capture_id}/review`, {
        method: "POST",
        body: form,
      });
      onSaved();
    } catch (e: any) {
      onError(e.message);
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="editor">
      <div className="editor-toolbar">
        <div className="segmented">
          {["add", "erase", "pan"].map((t) => (
            <button
              key={t}
              className={tool === t ? "active" : ""}
              onClick={() => setTool(t)}
            >
              {t === "add" ? "+ Paper" : t === "erase" ? "− Background" : "Pan"}
            </button>
          ))}
        </div>
        <label>
          Brush{" "}
          <input
            aria-label="Brush size"
            type="range"
            min="1"
            max="60"
            value={radius}
            onChange={(e) => setRadius(+e.target.value)}
          />
          <span>{radius}px</span>
        </label>
        <button
          onClick={() => {
            const s = state.current;
            if (s.undo?.length) {
              s.mask.getContext("2d").putImageData(s.undo.pop(), 0, 0);
              draw();
            }
          }}
        >
          Undo
        </button>
        <button
          onClick={() => {
            pushUndo();
            const s = state.current;
            if (s.mask) {
              s.mask.getContext("2d").drawImage(s.original, 0, 0);
              draw();
            }
          }}
        >
          Reset mask
        </button>
      </div>
      <canvas
        ref={canvas}
        aria-label="Native-resolution mask editor"
        onContextMenu={(e) => e.preventDefault()}
        onPointerDown={(e) => {
          if (!loaded) return;
          e.currentTarget.setPointerCapture(e.pointerId);
          const s = state.current;
          const [x, y] = xy(e);
          s.drag = tool === "pan" || e.button === 1 ? "pan" : "paint";
          s.last = [x, y];
          s.erase = tool === "erase" || e.button === 2;
          s.previous = null;
          if (s.drag === "paint") {
            pushUndo();
            paint(x, y);
          }
        }}
        onPointerMove={(e) => {
          const s = state.current;
          if (!s.drag) return;
          const [x, y] = xy(e);
          if (s.drag === "pan") {
            s.x += x - s.last[0];
            s.y += y - s.last[1];
            s.last = [x, y];
            draw();
          } else paint(x, y);
        }}
        onPointerUp={() => {
          state.current.drag = null;
          state.current.previous = null;
        }}
        onPointerCancel={() => {
          state.current.drag = null;
        }}
        onWheel={(e) => {
          const s = state.current;
          if (!s.image) return;
          const r = e.currentTarget.getBoundingClientRect(),
            x = ((e.clientX - r.left) * e.currentTarget.width) / r.width,
            y = ((e.clientY - r.top) * e.currentTarget.height) / r.height;
          const n = Math.max(
            s.fit * 0.5,
            Math.min(s.fit * 20, s.scale * (e.deltaY < 0 ? 1.15 : 1 / 1.15)),
          );
          s.x = x - ((x - s.x) * n) / s.scale;
          s.y = y - ((y - s.y) * n) / s.scale;
          s.scale = n;
          setZoom(n / s.fit);
          draw();
        }}
      />
      <div className="editor-footer">
        <span>
          <button onClick={fit}>Fit view</button> {zoom.toFixed(1)}× · Wheel to
          zoom · Middle drag to pan
        </span>
        <label>
          <input
            type="checkbox"
            checked={fill}
            onChange={(e) => setFill(e.target.checked)}
          />{" "}
          Mask overlay
        </label>
        <button className="primary" disabled={!loaded || saving} onClick={save}>
          {saving ? "Checking mask…" : "Approve & save"}{" "}
          <Icon name="check" size={16} />
        </button>
      </div>
    </div>
  );
}

function App() {
  const [tab, setTab] = useState("jobs");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [versions, setVersions] = useState<any>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [files, setFiles] = useState<File[]>([]);
  const [name, setName] = useState("");
  const [review, setReview] = useState("flagged");
  const [confirmed, setConfirmed] = useState(false);
  const [expected, setExpected] = useState("");
  const [pieces, setPieces] = useState<Piece[]>([]);
  const [pieceId, setPieceId] = useState("");
  const [assets, setAssets] = useState<Artifact[]>([]);
  const [logs, setLogs] = useState<any[]>([]);
  const [showLogs, setShowLogs] = useState(false);
  const [showArtifacts, setShowArtifacts] = useState(false);
  const refresh = useCallback(async () => {
    try {
      const [list, v] = await Promise.all([
        api("/jobs"),
        api("/system/versions"),
      ]);
      setJobs(list);
      setVersions(v);
      if (selected) {
        const next = list.find((j: Job) => j.id === selected);
        if (next) setJob(next);
      }
    } catch (e: any) {
      setError(e.message);
    }
  }, [selected]);
  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [refresh]);
  const loadDetails = useCallback(async () => {
    if (!selected) return;
    try {
      const [p, a] = await Promise.all([
        api(`/jobs/${selected}/pieces`),
        api(`/jobs/${selected}/artifacts`),
      ]);
      setPieces(p);
      setPieceId((old) =>
        p.some((x: Piece) => x.capture_id === old)
          ? old
          : p[0]?.capture_id || "",
      );
      setAssets(Object.values(a));
    } catch (e: any) {
      setError(e.message);
    }
  }, [selected]);
  useEffect(() => {
    loadDetails();
  }, [loadDetails, job?.state, job?.attempt]);
  useEffect(() => {
    if (!selected) return;
    setLogs([]);
    const es = new EventSource(`/api/v1/jobs/${selected}/events`);
    es.addEventListener("progress", (e: any) => {
      const event = JSON.parse(e.data);
      if (event.kind === "log") setLogs((old) => [...old.slice(-149), event]);
      if (["state", "phase", "review"].includes(event.kind)) refresh();
    });
    return () => es.close();
  }, [selected]);
  const action = async (path: string) => {
    setError("");
    setBusy(true);
    try {
      await api(path, { method: "POST" });
      await refresh();
      await loadDetails();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };
  const open = (j: Job) => {
    setSelected(j.id);
    setJob(j);
    setTab("jobs");
    setError("");
  };
  const upload = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const form = new FormData();
      files.forEach((f) => form.append("files", f));
      form.set("name", name || "Untitled page");
      form.set("review", review);
      form.set("acquisition_confirmed", String(confirmed));
      form.set("expected_pieces", expected || "0");
      const value = await api("/jobs", { method: "POST", body: form });
      setFiles([]);
      setName("");
      setConfirmed(false);
      await refresh();
      open(value);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };
  const fileInput = useRef<HTMLInputElement>(null);
  const currentPiece = pieces.find((p) => p.capture_id === pieceId);
  const findAsset = (suffix: string) =>
    assets.find(
      (a) =>
        a.path ===
        `attempts/${String(job?.attempt).padStart(4, "0")}/${suffix}`,
    );
  const active = jobs.filter((j) =>
    ["running", "queued", "review_required", "cancelling"].includes(j.state),
  ).length;
  return (
    <div className="shell">
      <aside>
        <div className="brand">
          <div className="brand-icon">
            <Icon name="paper" size={26} />
          </div>
          <div>
            paperwork<span>RECONSTRUCTION STUDIO</span>
          </div>
        </div>
        <div className="workspace-label">WORKSPACE</div>
        <nav>
          <button
            className={tab === "jobs" ? "selected" : ""}
            onClick={() => {
              setTab("jobs");
              setSelected(null);
              setJob(null);
            }}
          >
            <Icon name="grid" /> Reconstructions <span>{jobs.length}</span>
          </button>
          <button
            className={tab === "new" ? "selected" : ""}
            onClick={() => {
              setTab("new");
              setError("");
            }}
          >
            <Icon name="plus" /> New reconstruction
          </button>
          <button
            className={tab === "versions" ? "selected" : ""}
            onClick={() => setTab("versions")}
          >
            <Icon name="layers" /> Pipeline versions
          </button>
        </nav>
        <div className="aside-bottom">
          <div className="system-dot">
            <i className={versions?.worker_ready ? "online" : ""} />{" "}
            {versions?.worker_ready
              ? "Pipeline ready"
              : "Connecting to pipeline"}
          </div>
          <span>CPU · 4 stages · Local workspace</span>
          <div className="aside-rule" />
          <p>Your captures and results stay in this workspace.</p>
        </div>
      </aside>
      <main>
        <header>
          <div className="breadcrumb">
            Workspace <span>/</span>{" "}
            {tab === "new"
              ? "New reconstruction"
              : tab === "versions"
                ? "Pipeline versions"
                : selected
                  ? "Reconstruction"
                  : "Reconstructions"}
          </div>
          <div className="local-pill">
            <span /> Local
          </div>
        </header>
        <div className="content">
          {error && (
            <div role="alert" className="error">
              <span>{error}</span>
              <button aria-label="Dismiss error" onClick={() => setError("")}>
                <Icon name="close" size={16} />
              </button>
            </div>
          )}
          {tab === "jobs" && !selected && (
            <>
              <div className="page-title">
                <div>
                  <div className="eyebrow">FROM FRAGMENTS TO A WHOLE</div>
                  <h1>Your reconstructions</h1>
                  <p>
                    Bring the pieces together. Follow every step along the way.
                  </p>
                </div>
                <button className="primary" onClick={() => setTab("new")}>
                  <Icon name="plus" size={18} /> New reconstruction
                </button>
              </div>
              <div className="stats">
                <div>
                  <span>ALL PAGES</span>
                  <strong>{jobs.length.toString().padStart(2, "0")}</strong>
                </div>
                <div>
                  <span>IN PROGRESS</span>
                  <strong>{active.toString().padStart(2, "0")}</strong>
                </div>
                <div>
                  <span>SUPPORTED ASSEMBLIES</span>
                  <strong>
                    {jobs
                      .filter((j) => j.outcome === "supported")
                      .length.toString()
                      .padStart(2, "0")}
                  </strong>
                </div>
              </div>
              <div className="section-top">
                <h2>Recent work</h2>
                <span>
                  {jobs.length} reconstruction{jobs.length !== 1 ? "s" : ""}
                </span>
              </div>
              {jobs.length ? (
                <div className="job-list">
                  {jobs.map((j) => (
                    <button
                      className="job-row"
                      key={j.id}
                      onClick={() => open(j)}
                    >
                      <div className="page-thumb">
                        <Icon name="paper" size={26} />
                      </div>
                      <div className="job-name">
                        <strong>{j.name}</strong>
                        <span>
                          {j.inventory?.pieces.length || j.request.files.length}{" "}
                          captures · {date(j.created)}
                        </span>
                      </div>
                      <div className="job-phase">
                        {labels[j.phase || ""] || "Waiting in queue"}
                      </div>
                      <Badge job={j} />
                      <Icon name="arrow" size={18} />
                    </button>
                  ))}
                </div>
              ) : (
                <div className="empty">
                  <div className="empty-art">
                    <span />
                    <span />
                    <span />
                    <span />
                  </div>
                  <h2>Every piece has a place.</h2>
                  <p>
                    Add captures of a torn page to start your first
                    reconstruction.
                  </p>
                  <button className="primary" onClick={() => setTab("new")}>
                    Create a reconstruction <Icon name="arrow" size={16} />
                  </button>
                  <small>Images or PDFs · Native resolution preserved</small>
                </div>
              )}
              <div className="pipeline-note">
                <Icon name="layers" />
                <div>
                  <strong>One continuous workflow</strong>
                  <span>
                    Segmentation → edge observations → pair placements →
                    verified assembly
                  </span>
                </div>
              </div>
            </>
          )}
          {tab === "new" && (
            <>
              <button className="text-button" onClick={() => setTab("jobs")}>
                <Icon name="back" size={16} /> All reconstructions
              </button>
              <div className="page-title">
                <div>
                  <div className="eyebrow">START WITH YOUR CAPTURES</div>
                  <h1>New reconstruction</h1>
                  <p>One complete page. One capture for each physical piece.</p>
                </div>
              </div>
              <form onSubmit={upload} className="new-layout">
                <section className="panel">
                  <label className="field-label" htmlFor="page-name">
                    Page name
                  </label>
                  <input
                    id="page-name"
                    required
                    maxLength={120}
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder="e.g. Notebook page, October 28"
                  />
                  <div className="field-label spaced">Capture files</div>
                  <input
                    ref={fileInput}
                    className="hidden"
                    type="file"
                    accept=".png,.jpg,.jpeg,.tif,.tiff,.bmp,.pdf"
                    multiple
                    onChange={(e) => setFiles(Array.from(e.target.files || []))}
                  />
                  <button
                    type="button"
                    className="dropzone"
                    onClick={() => fileInput.current?.click()}
                    onDragOver={(e) => e.preventDefault()}
                    onDrop={(e) => {
                      e.preventDefault();
                      setFiles(Array.from(e.dataTransfer.files));
                    }}
                  >
                    <div className="upload-icon">
                      <Icon name="upload" size={25} />
                    </div>
                    <strong>
                      {files.length
                        ? `${files.length} files selected`
                        : "Drop your captures here"}
                    </strong>
                    <span>or click to browse</span>
                    <small>
                      PNG, JPEG, TIFF, BMP or PDF · Up to 250 MB per batch
                    </small>
                  </button>
                  {files.length > 0 && (
                    <div className="file-list">
                      {files.map((f, i) => (
                        <div key={i}>
                          <Icon name="paper" size={16} />
                          <span>{f.name}</span>
                          <small>{(f.size / 1024).toFixed(0)} KB</small>
                          <button
                            type="button"
                            aria-label={`Remove ${f.name}`}
                            onClick={() =>
                              setFiles((old) => old.filter((_, n) => n !== i))
                            }
                          >
                            <Icon name="close" size={14} />
                          </button>
                        </div>
                      ))}
                    </div>
                  )}
                  <div className="form-columns">
                    <label>
                      <span className="field-label">Mask review</span>
                      <select
                        value={review}
                        onChange={(e) => setReview(e.target.value)}
                      >
                        <option value="flagged">Review flagged pieces</option>
                        <option value="all">Review every piece</option>
                        <option value="none">
                          Automatic structural acceptance
                        </option>
                      </select>
                    </label>
                    <label>
                      <span className="field-label">
                        Expected physical pieces
                      </span>
                      <input
                        type="number"
                        min="1"
                        max="60"
                        value={expected}
                        onChange={(e) => setExpected(e.target.value)}
                        placeholder="Count from files / PDF pages"
                      />
                    </label>
                  </div>
                  <p className="help">
                    PDF pages are rendered at 300 DPI. Each page should contain
                    one piece. Captures are never independently resized.
                  </p>
                  <label className="declaration">
                    <input
                      type="checkbox"
                      required
                      checked={confirmed}
                      onChange={(e) => setConfirmed(e.target.checked)}
                    />
                    <span>
                      I have all pieces from one rectangular page, captured at a
                      common pixel scale without perspective distortion or
                      individual resizing.
                    </span>
                  </label>
                  <div className="form-bottom">
                    <span>
                      <Icon name="clock" size={16} /> Runs in the background
                    </span>
                    <button
                      className="primary"
                      disabled={busy || !files.length || !confirmed}
                    >
                      {busy ? "Preparing…" : "Start reconstruction"}{" "}
                      <Icon name="arrow" size={16} />
                    </button>
                  </div>
                </section>
                <section className="capture-guide">
                  <div className="eyebrow">A GOOD START</div>
                  <h2>Keep the original scale.</h2>
                  <p>
                    Same-DPI scans or flat, parallel captures at a fixed scale
                    work with this pipeline.
                  </p>
                  <ol>
                    <li>
                      <strong>One piece per capture</strong>
                      <span>
                        Leave a visible background margin around every edge.
                      </span>
                    </li>
                    <li>
                      <strong>Keep every piece</strong>
                      <span>
                        Upload all pieces from the same complete page.
                      </span>
                    </li>
                    <li>
                      <strong>Review when needed</strong>
                      <span>
                        You can correct the mask before assembly begins.
                      </span>
                    </li>
                  </ol>
                  <div className="small-note">
                    A capture declaration records your acquisition settings; it
                    does not calibrate a camera.
                  </div>
                </section>
              </form>
            </>
          )}
          {tab === "jobs" && selected && job && (
            <>
              <button
                className="text-button"
                onClick={() => {
                  setSelected(null);
                  setJob(null);
                }}
              >
                <Icon name="back" size={16} /> All reconstructions
              </button>
              <div className="page-title">
                <div>
                  <div className="eyebrow">
                    RECONSTRUCTION · ATTEMPT{" "}
                    {job.attempt.toString().padStart(2, "0")}
                  </div>
                  <h1>{job.name}</h1>
                  <p>
                    {date(job.created)} ·{" "}
                    {job.inventory?.pieces.length || job.request.files.length}{" "}
                    pieces · <code>{job.id.slice(0, 8)}</code>
                  </p>
                </div>
                <div className="actions">
                  <Badge job={job} />
                  {job.state === "completed" && (
                    <button disabled={busy} onClick={() => action(`/jobs/${job.id}/assembly-attempts`)}>
                      Run assembly again
                    </button>
                  )}
                  {[
                    "queued",
                    "running",
                    "review_required",
                    "cancelling",
                  ].includes(job.state) && (
                    <button
                      disabled={busy || job.state === "cancelling"}
                      onClick={() => action(`/jobs/${job.id}/cancel`)}
                    >
                      Cancel
                    </button>
                  )}
                  {["failed", "cancelled", "interrupted"].includes(
                    job.state,
                  ) && (
                    <button
                      className="primary"
                      disabled={busy}
                      onClick={() => action(`/jobs/${job.id}/resume`)}
                    >
                      Verify & resume
                    </button>
                  )}
                  {!["running", "queued", "cancelling"].includes(job.state) && (
                    <a
                      className="button"
                      href={`/api/v1/jobs/${job.id}/export`}
                    >
                      <Icon name="download" size={16} /> Export job
                    </a>
                  )}
                </div>
              </div>
              <div className="stage-track">
                {phases.map((phase, i) => {
                  const done = job.stages.some(
                    (s) => s.phase === phase && ["ready", "reused"].includes(s.status),
                  );
                  return (
                    <div
                      className={
                        done ? "done" : job.phase === phase ? "current" : ""
                      }
                      key={phase}
                    >
                      <b>
                        {done ? (
                          <Icon name="check" size={14} />
                        ) : (
                          String(i + 1).padStart(2, "0")
                        )}
                      </b>
                      <span>{labels[phase]}</span>
                    </div>
                  );
                })}
              </div>
              {job.detail && (
                <div
                  className={
                    "notice " + (job.state === "failed" ? "failed" : "")
                  }
                >
                  <strong>
                    {job.state === "review_required"
                      ? "Your review is needed"
                      : readable(job.state)}
                  </strong>
                  <p>{job.detail}</p>
                </div>
              )}
              {job.state === "review_required" && (
                <>
                  <div className="section-top">
                    <div>
                      <h2>Review piece masks</h2>
                      <p>
                        Correct the green mask, then approve each pending piece.
                      </p>
                    </div>
                    <button
                      className="primary"
                      disabled={
                        busy || pieces.some((p) => !p.accepted && !p.approved)
                      }
                      onClick={() => action(`/jobs/${job.id}/review-revisions`)}
                    >
                      Continue with reviewed masks{" "}
                      <Icon name="arrow" size={16} />
                    </button>
                  </div>
                  <div className="review-layout">
                    <div className="piece-list">
                      {pieces.map((p, i) => (
                        <button
                          className={pieceId === p.capture_id ? "active" : ""}
                          key={p.capture_id}
                          onClick={() => setPieceId(p.capture_id)}
                        >
                          <img alt="Predicted piece mask" src={p.overlay} />
                          <span>
                            Piece {String(i + 1).padStart(2, "0")}
                            <small>
                              {p.approved
                                ? "Approved"
                                : p.accepted
                                  ? "Auto accepted"
                                  : "Needs review"}
                            </small>
                          </span>
                          {p.approved && <Icon name="check" size={15} />}
                        </button>
                      ))}
                    </div>
                    <div>
                      {currentPiece && (
                        <>
                          <div className="piece-reasons">
                            {currentPiece.reason_codes.map((r) => (
                              <span key={r}>{readable(r.toLowerCase())}</span>
                            ))}
                          </div>
                          <MaskEditor
                            key={currentPiece.capture_id}
                            piece={currentPiece}
                            jobId={job.id}
                            onSaved={async () => {
                              await loadDetails();
                              await refresh();
                            }}
                            onError={setError}
                          />
                        </>
                      )}
                    </div>
                  </div>
                </>
              )}
              {job.state !== "review_required" && (
                <div className="result-grid">
                  <section className="preview-panel">
                    {findAsset("stage4/preview.png") ? (
                      <img
                        className="assembly-preview"
                        alt="Published reconstruction preview"
                        src={findAsset("stage4/preview.png")!.url}
                      />
                    ) : (
                      <div className="working-state">
                        <div
                          className={
                            "working-mark " +
                            (job.state === "running" ? "spinning" : "")
                          }
                        >
                          <Icon name="layers" size={34} />
                        </div>
                        <h2>
                          {job.state === "running"
                            ? labels[job.phase || ""] || "Processing page"
                            : job.state === "queued"
                              ? "Ready for its turn"
                              : job.state === "failed"
                                ? "Execution stopped"
                                : "No published preview yet"}
                        </h2>
                        <p>
                          {job.state === "running"
                            ? "The pipeline continues even if you close this page."
                            : "Stage outputs and diagnostic logs are retained below."}
                        </p>
                        {job.state === "running" && (
                          <span className="live-status">
                            <i /> Live stage activity
                          </span>
                        )}
                      </div>
                    )}
                  </section>
                  <section className="assessment-panel">
                    <div className="eyebrow">INDEPENDENT ASSESSMENT</div>
                    <h2>
                      {job.summary
                        ? readable(job.summary.outcome)
                        : "Awaiting publication"}
                    </h2>
                    <p>
                      {job.summary?.outcome === "supported"
                        ? "All pieces form one connected assembly with verified native support and physical checks."
                        : "The published assessment determines whether the assembly is supported, partial or unresolved."}
                    </p>
                    <dl>
                      <div>
                        <dt>Physical pieces</dt>
                        <dd>
                          {job.summary?.pieces ||
                            job.inventory?.pieces.length ||
                            "—"}
                        </dd>
                      </div>
                      <div>
                        <dt>Selected joins</dt>
                        <dd>{job.summary?.joins ?? "—"}</dd>
                      </div>
                      <div>
                        <dt>Connected</dt>
                        <dd>
                          {job.summary
                            ? job.summary.connected
                              ? "Yes"
                              : "Separate components"
                            : "—"}
                        </dd>
                      </div>
                      <div>
                        <dt>Termination</dt>
                        <dd>
                          {job.summary?.termination
                            ? readable(job.summary.termination)
                            : "—"}
                        </dd>
                      </div>
                    </dl>
                    {job.summary?.reason_codes?.map((r: string) => (
                      <div className="reason" key={r}>
                        {readable(r)}
                      </div>
                    ))}
                    <p className="help">
                      Support is an observable assessment. It does not certify
                      unique content order or pixel-perfect placement.
                    </p>
                    {findAsset("stage4/result.json") && (
                      <a
                        className="text-button"
                        href={findAsset("stage4/result.json")!.url}
                        target="_blank"
                      >
                        View result JSON <Icon name="arrow" size={14} />
                      </a>
                    )}
                    {findAsset("stage4.assessment/report.md") && (
                      <a
                        className="text-button"
                        href={findAsset("stage4.assessment/report.md")!.url}
                        target="_blank"
                      >
                        Assessment report <Icon name="arrow" size={14} />
                      </a>
                    )}
                  </section>
                </div>
              )}
              <section className="diagnostics">
                <button
                  className="disclosure"
                  onClick={() => setShowLogs(!showLogs)}
                >
                  <span>Stage activity & logs</span>
                  <span>{showLogs ? "−" : "+"}</span>
                </button>
                {showLogs && (
                  <>
                    <div className="log">
                      <pre>
                        {logs
                          .map(
                            (l) =>
                              `${new Date(l.at).toLocaleTimeString()}  [${l.data.phase}] ${l.data.line}`,
                          )
                          .join("\n") || "Waiting for stage output…"}
                      </pre>
                    </div>
                    <div className="log-files">
                      {assets
                        .filter((a) => a.path.endsWith(".log"))
                        .map((a) => (
                          <a key={a.path} href={a.url} target="_blank">
                            {a.path.split("/").pop()}
                          </a>
                        ))}
                    </div>
                  </>
                )}
                <button
                  className="disclosure"
                  onClick={() => setShowArtifacts(!showArtifacts)}
                >
                  <span>Artifacts & provenance</span>
                  <span>{showArtifacts ? "−" : "+"}</span>
                </button>
                {showArtifacts && (
                  <div className="artifact-table">
                    {assets
                      .filter((a) =>
                        /summary.json|manifest.json|assessment.json|result.json|pipeline.lock.json|request.json|recovery_request.json/.test(
                          a.path,
                        ),
                      )
                      .map((a) => (
                        <a key={a.path} href={a.url} target="_blank">
                          <span>{a.path}</span>
                          <small>
                            {(a.size / 1024).toFixed(1)} KB{" "}
                            <Icon name="download" size={13} />
                          </small>
                        </a>
                      ))}
                  </div>
                )}
              </section>
            </>
          )}
          {tab === "versions" && versions && (
            <>
              <div className="page-title">
                <div>
                  <div className="eyebrow">THIS INSTALLED IMAGE</div>
                  <h1>Pipeline versions</h1>
                  <p>
                    Four independently maintained stages. One fixed,
                    reproducible composition.
                  </p>
                </div>
                <span className="version-chip">APP {versions.application}</span>
              </div>
              <div className="version-cards">
                {Object.entries(versions.lock.components).map(
                  ([key, c]: [string, any], i) => (
                    <section className="panel version-card" key={key}>
                      <span className="eyebrow">STAGE {i + 1}</span>
                      <h2>
                        {
                          [
                            "Segmentation",
                            "Boundary observations",
                            "Pair placements",
                            "Global assembly",
                          ][i]
                        }
                      </h2>
                      <div className="release-version">
                        v{c.distribution_version}
                      </div>
                      <code>{c.commit.slice(0, 12)}</code>
                      <p>{c.reference}</p>
                      <span className="version-state">
                        {c.publication.kind === "draft_pull_request"
                          ? "Pinned integration PR"
                          : "Published release"}
                      </span>
                      <a
                        href={c.source_readme}
                        target="_blank"
                        rel="noreferrer"
                      >
                        Source & contract <Icon name="arrow" size={14} />
                      </a>
                    </section>
                  ),
                )}
              </div>
              <section className="panel version-explanation">
                <h2>Updates create a new image.</h2>
                <p>
                  Use the build command to resolve compatible stage versions and
                  qualify the candidate. Existing jobs keep their original
                  composition. This running application does not install or
                  replace stage code.
                </p>
                <pre>.\scripts\build.ps1 -LatestCompatible</pre>
                <details>
                  <summary>View complete composition lock</summary>
                  <pre>{JSON.stringify(versions.lock, null, 2)}</pre>
                </details>
                <div className="help">
                  Lock SHA-256: <code>{versions.lock_sha256}</code>
                </div>
              </section>
            </>
          )}
          <footer>
            Paperwork{" "}
            <span>
              Built to keep every piece, every stage, and every decision
              traceable.
            </span>
          </footer>
        </div>
      </main>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
