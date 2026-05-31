function heatColor(score) {
  if (score >= 80) return "#ef4444";
  if (score >= 60) return "#f97316";
  if (score >= 40) return "#f59e0b";
  if (score >= 20) return "#84cc16";
  return "#22c55e";
}

export default function Heatmap({ heatmap }) {
  if (!heatmap) return (
    <div style={card}>
      <h3 style={title}>Zone Heatmap</h3>
      <p style={{ color: "#64748b", textAlign: "center", marginTop: 40 }}>Loading…</p>
    </div>
  );

  return (
    <div style={card}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <h3 style={title}>Zone Heatmap</h3>
        {!heatmap.data_confidence &&
          <span style={{ fontSize: 11, color: "#f59e0b", background: "#1a1500",
                         padding: "2px 8px", borderRadius: 4 }}>
            Low confidence
          </span>
        }
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8, marginTop: 16 }}>
        {heatmap.zones.map((z) => {
          const col = heatColor(z.normalised_score);
          return (
            <div key={z.zone_id} style={{
              background: "#2d2d3d", borderRadius: 8, padding: "10px 12px",
              borderTop: `3px solid ${col}`,
            }}>
              <div style={{ fontSize: 12, fontWeight: 600, color: "#cbd5e1",
                            overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {z.zone_id}
              </div>
              <div style={{ fontSize: 20, fontWeight: 700, color: col, margin: "4px 0" }}>
                {z.normalised_score.toFixed(0)}
              </div>
              <div style={{ fontSize: 11, color: "#475569" }}>
                {z.visit_count} visits · {(z.avg_dwell_ms / 1000).toFixed(0)}s avg
              </div>
            </div>
          );
        })}
      </div>
      {heatmap.zones.length === 0 &&
        <p style={{ color: "#475569", textAlign: "center", marginTop: 32 }}>
          No zone data yet
        </p>
      }
    </div>
  );
}

const card  = { background: "#1e1e2e", borderRadius: 12, padding: "20px 24px",
                boxShadow: "0 2px 8px rgba(0,0,0,0.4)" };
const title = { margin: 0, fontSize: 15, fontWeight: 600, color: "#e2e8f0" };