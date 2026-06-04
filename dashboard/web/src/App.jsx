import { useState, useEffect, useCallback } from "react";

const API = import.meta.env.VITE_API_URL || "http://localhost:8000";

const STORES = [
  { id: "ST1008", label: "ST1008 — Brigade Bangalore" },
  { id: "ST1076", label: "ST1076 — Purplle Mumbai" },
];

// Human-readable zone name map
const ZONE_LABELS = {
  MAKEUP_ZONE:                      "Makeup Zone",
  SKIN_ZONE:                        "Skincare Zone",
  SKINCARE_ZONE:                    "Skincare Zone",
  HAIR_ZONE:                        "Hair Zone",
  BATH_BODY_ZONE:                   "Bath & Body",
  PERSONAL_CARE_ZONE:               "Personal Care",
  FRAGRANCE_ZONE:                   "Fragrance",
  BILLING_QUEUE:                    "Billing Queue",
  BILLING_COUNTER:                  "Billing Counter",
  ENTRY_THRESHOLD:                  "Entry",
  REST_AREA:                        "Staff Area",
  PURPLLE_MUM_1076_Z01:             "Left Shelf",
  PURPLLE_MUM_1076_Z02:             "Center Display",
  PURPLLE_MUM_1076_Z03:             "Lipstick Aisle",
  PURPLLE_MUM_1076_Z04:             "Makeup Point",
  PURPLLE_MUM_1076_Z05:             "Back Wall Display",
  PURPLLE_MUM_1076_Z_ENTRY:         "Entry",
  PURPLLE_MUM_1076_Z_BILLING_01:    "Billing Queue",
  PURPLLE_MUM_1076_Z_BILLING_COUNTER: "Billing Counter",
  PURPLLE_MUM_1076_Z_BOH:           "Back of House",
};

function zoneLabel(id) {
  if (!id) return id;
  return ZONE_LABELS[id] || id.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
}

function fmt(ms) {
  if (!ms || ms === 0) return "0s";
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  return `${Math.floor(s / 60)}m ${s % 60}s`;
}

function pct(v) {
  return (v * 100).toFixed(1) + "%";
}

function scoreColor(score) {
  if (score >= 75) return "#ef4444";
  if (score >= 40) return "#f59e0b";
  return "#22c55e";
}

function useFetch(url, interval = 10000) {
  const [data, setData] = useState(null);
  const [err, setErr]   = useState(null);

  const load = useCallback(() => {
    fetch(url)
      .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(d => { setData(d); setErr(null); })
      .catch(e => setErr(e.message));
  }, [url]);

  useEffect(() => {
    load();
    const id = setInterval(load, interval);
    return () => clearInterval(id);
  }, [load, interval]);

  return { data, err };
}

// ── Sub-components ─────────────────────────────────────────────────────────

function StatCard({ value, label, sub, accent }) {
  const colors = {
    blue:   { border: "#6366f1", text: "#818cf8" },
    green:  { border: "#22c55e", text: "#4ade80" },
    orange: { border: "#f59e0b", text: "#fbbf24" },
    red:    { border: "#ef4444", text: "#f87171" },
  };
  const c = colors[accent] || colors.blue;
  return (
    <div style={{
      background: "#0f1117",
      border: `1px solid ${c.border}33`,
      borderLeft: `3px solid ${c.border}`,
      borderRadius: 10,
      padding: "20px 24px",
      flex: 1,
      minWidth: 160,
    }}>
      <div style={{ fontSize: 38, fontWeight: 700, color: c.text, fontFamily: "'DM Mono', monospace" }}>
        {value}
      </div>
      <div style={{ fontSize: 13, color: "#9ca3af", marginTop: 4 }}>{label}</div>
      {sub && <div style={{ fontSize: 12, color: c.text, marginTop: 6 }}>{sub}</div>}
    </div>
  );
}

function FunnelBar({ stage, count, maxCount, dropOff, isFirst }) {
  const width = maxCount > 0 ? Math.max((count / maxCount) * 100, count > 0 ? 4 : 0) : 0;
  const labels = {
    entry:         "Entry",
    zone_visit:    "Zone Visit",
    billing_queue: "Billing Queue",
    purchase:      "Purchase",
  };
  return (
    <div style={{ marginBottom: 16 }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 6 }}>
        <span style={{ fontSize: 14, color: "#d1d5db" }}>{labels[stage] || stage}</span>
        <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
          {!isFirst && dropOff > 0 && (
            <span style={{ fontSize: 12, color: "#ef4444" }}>
              ↓{dropOff.toFixed(1)}%
            </span>
          )}
          <span style={{ fontSize: 14, fontWeight: 600, color: "#f9fafb", fontFamily: "'DM Mono', monospace" }}>
            {count}
          </span>
        </div>
      </div>
      <div style={{ background: "#1f2937", borderRadius: 4, height: 8 }}>
        <div style={{
          width: `${width}%`,
          height: "100%",
          borderRadius: 4,
          background: "linear-gradient(90deg, #6366f1, #8b5cf6)",
          transition: "width 0.5s ease",
        }} />
      </div>
    </div>
  );
}

function ZoneCard({ zone_id, normalised_score, visit_count, avg_dwell_ms }) {
  const color = scoreColor(normalised_score);
  return (
    <div style={{
      background: "#0f1117",
      border: "1px solid #1f2937",
      borderRadius: 8,
      padding: "14px 16px",
    }}>
      <div style={{ fontSize: 12, color: "#9ca3af", marginBottom: 6, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
        {zoneLabel(zone_id)}
      </div>
      <div style={{ fontSize: 28, fontWeight: 700, color, fontFamily: "'DM Mono', monospace" }}>
        {Math.round(normalised_score)}
      </div>
      <div style={{ fontSize: 11, color: "#6b7280", marginTop: 4 }}>
        {visit_count} visits · {fmt(avg_dwell_ms)} avg
      </div>
    </div>
  );
}

function AnomalyCard({ anomaly }) {
  const sev = {
    WARN:     { bg: "#78350f22", border: "#f59e0b", badge: "#f59e0b", text: "#fbbf24" },
    CRITICAL: { bg: "#7f1d1d22", border: "#ef4444", badge: "#ef4444", text: "#f87171" },
    INFO:     { bg: "#1e3a5f22", border: "#3b82f6", badge: "#3b82f6", text: "#60a5fa" },
  };
  const s = sev[anomaly.severity] || sev.INFO;
  const time = anomaly.detected_at
    ? new Date(anomaly.detected_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : "";
  return (
    <div style={{
      background: s.bg,
      border: `1px solid ${s.border}55`,
      borderRadius: 8,
      padding: "14px 18px",
      display: "flex",
      justifyContent: "space-between",
      alignItems: "flex-start",
      gap: 16,
    }}>
      <div>
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 6 }}>
          <span style={{
            background: s.badge,
            color: "#000",
            fontSize: 11,
            fontWeight: 700,
            padding: "2px 8px",
            borderRadius: 4,
          }}>{anomaly.severity}</span>
          <span style={{ fontSize: 14, fontWeight: 600, color: "#f9fafb" }}>
            {anomaly.anomaly_type.replace(/_/g, " ")}
          </span>
        </div>
        <div style={{ fontSize: 13, color: "#d1d5db" }}>{anomaly.description}</div>
        <div style={{ fontSize: 12, color: s.text, marginTop: 4 }}>
          → {anomaly.suggested_action}
        </div>
      </div>
      <div style={{ fontSize: 12, color: "#6b7280", whiteSpace: "nowrap" }}>{time}</div>
    </div>
  );
}

function EventBreakdownTable({ storeId }) {
  const { data } = useFetch(`${API}/stores/${storeId}/metrics`, 15000);

  // Pull event type counts from metrics avg_dwell (we derive counts from heatmap visit_count)
  // Instead show a simple well-known breakdown from what we know
  const rows = data?.avg_dwell_per_zone?.map(z => ({
    zone: zoneLabel(z.zone_id),
    visits: z.visit_count,
    dwell: fmt(z.avg_dwell_ms),
  })) || [];

  if (!rows.length) return null;

  return (
    <div style={{
      background: "#0a0d14",
      border: "1px solid #1f2937",
      borderRadius: 10,
      padding: "20px 24px",
    }}>
      <div style={{ fontSize: 14, fontWeight: 600, color: "#9ca3af", marginBottom: 16, letterSpacing: "0.05em", textTransform: "uppercase" }}>
        Zone Dwell Detail
      </div>
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr>
            {["Zone", "Visits", "Avg Dwell"].map(h => (
              <th key={h} style={{ textAlign: "left", fontSize: 12, color: "#6b7280", paddingBottom: 10, borderBottom: "1px solid #1f2937", fontWeight: 500 }}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} style={{ borderBottom: "1px solid #111827" }}>
              <td style={{ padding: "10px 0", fontSize: 13, color: "#d1d5db" }}>{r.zone}</td>
              <td style={{ padding: "10px 0", fontSize: 13, color: "#9ca3af", fontFamily: "'DM Mono', monospace" }}>{r.visits}</td>
              <td style={{ padding: "10px 0", fontSize: 13, color: "#9ca3af", fontFamily: "'DM Mono', monospace" }}>{r.dwell}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Main App ───────────────────────────────────────────────────────────────

export default function App() {
  const [storeId, setStoreId] = useState("ST1008");
  const [now, setNow]         = useState(new Date());

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  const { data: health }   = useFetch(`${API}/health`,                    15000);
  const { data: metrics }  = useFetch(`${API}/stores/${storeId}/metrics`, 10000);
  const { data: funnel }   = useFetch(`${API}/stores/${storeId}/funnel`,  10000);
  const { data: heatmap }  = useFetch(`${API}/stores/${storeId}/heatmap`, 10000);
  const { data: anomalies} = useFetch(`${API}/stores/${storeId}/anomalies`,10000);

  const storeHealth = health?.stores?.find(s => s.store_id === storeId);
  const isStale     = storeHealth?.status === "STALE_FEED";
  const dbOk        = health?.db_connected !== false;
  const lastEvent   = storeHealth?.last_event_at
    ? new Date(storeHealth.last_event_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })
    : "—";

  const activeAnomalies = anomalies?.anomalies || [];
  const zones           = heatmap?.zones || [];
  const funnelStages    = funnel?.stages || [];
  const maxCount        = Math.max(...funnelStages.map(s => s.count), 1);

  const queueDepth  = metrics?.current_queue_depth || 0;
  const queueHigh   = queueDepth >= 5;

  return (
    <div style={{
      minHeight: "100vh",
      background: "#060810",
      color: "#f9fafb",
      fontFamily: "'DM Sans', 'Inter', sans-serif",
      padding: "0 0 40px",
    }}>
      {/* ── Header ─────────────────────────────────────────────────────── */}
      <div style={{
        display: "flex",
        justifyContent: "space-between",
        alignItems: "center",
        padding: "20px 32px",
        borderBottom: "1px solid #1f2937",
        background: "#080b12",
      }}>
        <div>
          <div style={{ fontSize: 22, fontWeight: 700, color: "#f9fafb", letterSpacing: "-0.02em" }}>
            Store Intelligence
          </div>
          <div style={{ fontSize: 13, color: "#6b7280", marginTop: 2 }}>
            {storeId} — Updated {now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
          </div>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <select
            value={storeId}
            onChange={e => setStoreId(e.target.value)}
            style={{
              background: "#0f1117",
              border: "1px solid #374151",
              borderRadius: 8,
              color: "#f9fafb",
              padding: "8px 14px",
              fontSize: 14,
              cursor: "pointer",
              outline: "none",
            }}
          >
            {STORES.map(s => (
              <option key={s.id} value={s.id}>{s.label}</option>
            ))}
          </select>
          <div style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            fontSize: 13,
            color: "#4ade80",
          }}>
            <div style={{
              width: 8, height: 8, borderRadius: "50%",
              background: "#4ade80",
              boxShadow: "0 0 6px #4ade80",
              animation: "pulse 2s infinite",
            }} />
            Live
          </div>
        </div>
      </div>

      <div style={{ maxWidth: 1400, margin: "0 auto", padding: "24px 32px" }}>

        {/* ── Status bar ─────────────────────────────────────────────── */}
        <div style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          padding: "10px 16px",
          background: "#0a0d14",
          border: "1px solid #1f2937",
          borderRadius: 8,
          marginBottom: 24,
          fontSize: 13,
        }}>
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <div style={{
              width: 8, height: 8, borderRadius: "50%",
              background: isStale ? "#ef4444" : "#22c55e",
            }} />
            <span style={{ color: isStale ? "#f87171" : "#4ade80", fontWeight: 600 }}>
              {isStale ? "STALE_FEED" : "LIVE"}
            </span>
            <span style={{ color: "#6b7280" }}>last event: {lastEvent}</span>
          </div>
          <div style={{ color: dbOk ? "#4ade80" : "#f87171" }}>
            DB: {dbOk ? "connected" : "disconnected"}
          </div>
        </div>

        {/* ── Stat cards ─────────────────────────────────────────────── */}
        <div style={{ display: "flex", gap: 16, marginBottom: 24, flexWrap: "wrap" }}>
          <StatCard
            value={metrics?.unique_visitors ?? "—"}
            label="Unique Visitors"
            accent="blue"
          />
          <StatCard
            value={metrics ? pct(metrics.conversion_rate) : "—"}
            label="Conversion Rate"
            accent="green"
          />
          <StatCard
            value={queueDepth}
            label="Queue Depth"
            sub={queueHigh ? "High — open additional counter" : null}
            accent={queueHigh ? "orange" : "green"}
          />
          <StatCard
            value={metrics ? pct(metrics.abandonment_rate) : "—"}
            label="Abandonment Rate"
            accent={metrics?.abandonment_rate > 0.2 ? "red" : "green"}
          />
        </div>

        {/* ── Funnel + Heatmap ───────────────────────────────────────── */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1.4fr", gap: 20, marginBottom: 20 }}>

          {/* Funnel */}
          <div style={{
            background: "#0a0d14",
            border: "1px solid #1f2937",
            borderRadius: 10,
            padding: "20px 24px",
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
              <div style={{ fontSize: 14, fontWeight: 600, color: "#9ca3af", letterSpacing: "0.05em", textTransform: "uppercase" }}>
                Conversion Funnel
              </div>
              <div style={{ fontSize: 12, color: "#6b7280" }}>
                {funnel?.session_count || 0} sessions
              </div>
            </div>
            {funnelStages.map((s, i) => (
              <FunnelBar
                key={s.stage}
                stage={s.stage}
                count={s.count}
                maxCount={maxCount}
                dropOff={s.drop_off_pct}
                isFirst={i === 0}
              />
            ))}
            <div style={{ fontSize: 12, color: "#4b5563", marginTop: 12 }}>
              {funnel?.session_count || 0} total sessions
            </div>
            {storeId === "ST1076" && (
              <div style={{ fontSize: 11, color: "#4b5563", marginTop: 8, borderTop: "1px solid #1f2937", paddingTop: 8 }}>
                Note: Zone Visit &amp; Billing are counted from their respective cameras independently — visitor IDs differ across cameras in batch processing.
              </div>
            )}
          </div>

          {/* Heatmap */}
          <div style={{
            background: "#0a0d14",
            border: "1px solid #1f2937",
            borderRadius: 10,
            padding: "20px 24px",
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
              <div style={{ fontSize: 14, fontWeight: 600, color: "#9ca3af", letterSpacing: "0.05em", textTransform: "uppercase" }}>
                Zone Heatmap
              </div>
              {heatmap?.data_confidence === false && (
                <span style={{
                  fontSize: 11, color: "#f59e0b",
                  border: "1px solid #f59e0b55",
                  borderRadius: 4,
                  padding: "2px 8px",
                }}>Low confidence</span>
              )}
            </div>
            <div style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(140px, 1fr))",
              gap: 12,
            }}>
              {zones.map(z => (
                <ZoneCard key={z.zone_id} {...z} />
              ))}
              {!zones.length && (
                <div style={{ color: "#4b5563", fontSize: 13, gridColumn: "1/-1" }}>No zone data yet</div>
              )}
            </div>
          </div>
        </div>

        {/* ── Zone Dwell Table + Anomalies ───────────────────────────── */}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1.4fr", gap: 20, marginBottom: 20 }}>

          <EventBreakdownTable storeId={storeId} />

          {/* Anomalies */}
          <div style={{
            background: "#0a0d14",
            border: "1px solid #1f2937",
            borderRadius: 10,
            padding: "20px 24px",
          }}>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 16 }}>
              <div style={{ fontSize: 14, fontWeight: 600, color: "#9ca3af", letterSpacing: "0.05em", textTransform: "uppercase" }}>
                Active Anomalies
              </div>
              {activeAnomalies.length > 0 && (
                <span style={{
                  background: "#ef4444",
                  color: "#fff",
                  fontSize: 12,
                  fontWeight: 700,
                  padding: "2px 10px",
                  borderRadius: 20,
                }}>{activeAnomalies.length} active</span>
              )}
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {activeAnomalies.length === 0 && (
                <div style={{ color: "#4b5563", fontSize: 13 }}>No active anomalies</div>
              )}
              {activeAnomalies.map(a => (
                <AnomalyCard key={a.anomaly_id} anomaly={a} />
              ))}
            </div>
          </div>
        </div>

        {/* ── Footer ─────────────────────────────────────────────────── */}
        <div style={{ textAlign: "center", fontSize: 12, color: "#374151", marginTop: 8 }}>
          Store Intelligence · YOLOv8m + ByteTrack · SQLite · FastAPI · Refreshes every 10s
        </div>
      </div>

      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=DM+Mono:wght@400;500&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: #060810; }
        select option { background: #0f1117; }
        @keyframes pulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.4; }
        }
        ::-webkit-scrollbar { width: 6px; }
        ::-webkit-scrollbar-track { background: #0f1117; }
        ::-webkit-scrollbar-thumb { background: #374151; border-radius: 3px; }
      `}</style>
    </div>
  );
}