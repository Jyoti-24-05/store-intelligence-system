function heatColor(score) {
  if (score >= 80) return "#ef4444";
  if (score >= 60) return "#f97316";
  if (score >= 40) return "#f59e0b";
  if (score >= 20) return "#84cc16";
  return "#22c55e";
}

const card  = { background: "#1e1e2e", borderRadius: 12, padding: "20px 24px",
                boxShadow: "0 2px 8px rgba(0,0,0,0.4)" };
const title = { margin: 0, fontSize: 15, fontWeight: 600, color: "#e2e8f0" };

export default function Heatmap({ heatmap }) {
  if (!heatmap) return (
    <div style={card}>
      <h3 style={title}>Zone heatmap</h3>
      <p style={{ color: "#475569", textAlign: "center", marginTop: 40 }}>Loading…</p>
    </div>
  );

  const zones = heatmap.zones || [];

  return (
    <div style={card}>
      <div style={{ display: "flex", justifyContent: "space-between",
                    alignItems: "center", marginBottom: 16 }}>
        <h3 style={title}>Zone heatmap</h3>
        {!heatmap.data_confidence &&
          <span style={{ fontSize: 11, color: "#f59e0b", background: "#1a1500",
                         border: "1px solid rgba(245,158,11,0.4)",
                         padding: "2px 8px", borderRadius: 4 }}>
            Low confidence
          </span>
        }
      </div>

      {zones.length === 0
        ? <p style={{ color: "#475569", textAlign: "center", marginTop: 32, fontSize: 13 }}>
            No zone data yet
          </p>
        : <div style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))",
            gap: 8,
          }}>
            {zones.map((z) => {
              const col = heatColor(z.normalised_score);
              return (
                <div key={z.zone_id} style={{
                  background: "#2d2d3d", borderRadius: 8,
                  padding: "10px 12px", borderTop: `3px solid ${col}`,
                }}>
                  <div style={{ fontSize: 11, fontWeight: 600, color: "#cbd5e1",
                                overflow: "hidden", textOverflow: "ellipsis",
                                whiteSpace: "nowrap" }}>
                    {(z.zone_name || z.zone_id.replace(/_/g, " ")).toLowerCase()}
                  </div>
                  <div style={{ fontSize: 20, fontWeight: 700, color: col,
                                margin: "4px 0" }}>
                    {z.normalised_score.toFixed(0)}
                  </div>
                  <div style={{ fontSize: 11, color: "#475569" }}>
                    {z.visit_count} visits · {Math.round(z.avg_dwell_ms / 1000)}s avg
                  </div>
                </div>
              );
            })}
          </div>
      }
    </div>
  );
}