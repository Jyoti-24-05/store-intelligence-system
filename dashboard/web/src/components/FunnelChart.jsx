const STAGE_COLORS = {
  entry:         "#818cf8",
  zone_visit:    "#34d399",
  billing_queue: "#f59e0b",
  purchase:      "#22c55e",
};

export default function FunnelChart({ funnel }) {
  if (!funnel) return (
    <div style={card}>
      <h3 style={title}>Conversion Funnel</h3>
      <p style={{ color: "#64748b", textAlign: "center", marginTop: 40 }}>Loading…</p>
    </div>
  );

  const max = funnel.stages[0]?.count || 1;
  return (
    <div style={card}>
      <h3 style={title}>Conversion Funnel</h3>
      <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 16 }}>
        {funnel.stages.map((s) => {
          const pct  = max > 0 ? (s.count / max) * 100 : 0;
          const col  = STAGE_COLORS[s.stage] || "#94a3b8";
          const label = s.stage.replace("_", " ").replace(/\b\w/g, c => c.toUpperCase());
          return (
            <div key={s.stage}>
              <div style={{ display: "flex", justifyContent: "space-between",
                            fontSize: 13, marginBottom: 4 }}>
                <span style={{ color: "#cbd5e1" }}>{label}</span>
                <span style={{ color: col, fontWeight: 600 }}>
                  {s.count}
                  {s.stage !== "entry" && s.drop_off_pct > 0 &&
                    <span style={{ color: "#ef4444", marginLeft: 8, fontSize: 11 }}>
                      ↓{s.drop_off_pct.toFixed(1)}%
                    </span>
                  }
                </span>
              </div>
              <div style={{ height: 10, borderRadius: 5, background: "#2d2d3d" }}>
                <div style={{ width: `${pct}%`, height: "100%", borderRadius: 5,
                              background: col, transition: "width 0.6s ease" }} />
              </div>
            </div>
          );
        })}
      </div>
      <p style={{ fontSize: 12, color: "#475569", marginTop: 12 }}>
        {funnel.session_count} total sessions
      </p>
    </div>
  );
}

const card  = { background: "#1e1e2e", borderRadius: 12, padding: "20px 24px",
                boxShadow: "0 2px 8px rgba(0,0,0,0.4)" };
const title = { margin: 0, fontSize: 15, fontWeight: 600, color: "#e2e8f0" };