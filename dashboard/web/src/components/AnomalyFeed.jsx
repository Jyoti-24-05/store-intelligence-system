const SEV_STYLE = {
  CRITICAL: { bg: "#2d0a0a", border: "#ef4444", badge: "#ef4444", text: "CRITICAL" },
  WARN:     { bg: "#1f1400", border: "#f59e0b", badge: "#f59e0b", text: "WARN"     },
  INFO:     { bg: "#0a1628", border: "#38bdf8", badge: "#38bdf8", text: "INFO"     },
};

export default function AnomalyFeed({ anomalies }) {
  return (
    <div style={{ background: "#1e1e2e", borderRadius: 12, padding: "20px 24px",
                  boxShadow: "0 2px 8px rgba(0,0,0,0.4)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 16 }}>
        <h3 style={{ margin: 0, fontSize: 15, fontWeight: 600, color: "#e2e8f0" }}>
          Active Anomalies
        </h3>
        {anomalies.length > 0 &&
          <span style={{ fontSize: 12, background: "#3d0000", color: "#ef4444",
                         padding: "2px 10px", borderRadius: 99, fontWeight: 600 }}>
            {anomalies.length} active
          </span>
        }
      </div>

      {anomalies.length === 0
        ? <div style={{ display: "flex", alignItems: "center", gap: 8,
                        color: "#22c55e", fontSize: 14 }}>
            <span>✓</span><span>No active anomalies</span>
          </div>
        : <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {anomalies.map((a) => {
              const s = SEV_STYLE[a.severity] || SEV_STYLE.INFO;
              return (
                <div key={a.anomaly_id} style={{
                  background: s.bg, border: `1px solid ${s.border}`,
                  borderRadius: 8, padding: "12px 16px",
                  display: "flex", alignItems: "flex-start", gap: 12,
                }}>
                  <span style={{
                    fontSize: 11, fontWeight: 700, color: s.badge,
                    background: s.bg, border: `1px solid ${s.border}`,
                    padding: "2px 8px", borderRadius: 4, whiteSpace: "nowrap", marginTop: 1,
                  }}>
                    {s.text}
                  </span>
                  <div style={{ flex: 1 }}>
                    <div style={{ fontSize: 13, fontWeight: 600, color: "#e2e8f0",
                                  marginBottom: 2 }}>
                      {a.anomaly_type.replace(/_/g, " ")}
                    </div>
                    <div style={{ fontSize: 12, color: "#94a3b8" }}>{a.description}</div>
                    <div style={{ fontSize: 11, color: "#475569", marginTop: 4 }}>
                      → {a.suggested_action}
                    </div>
                  </div>
                  <span style={{ fontSize: 11, color: "#475569", whiteSpace: "nowrap" }}>
                    {new Date(a.detected_at).toLocaleTimeString()}
                  </span>
                </div>
              );
            })}
          </div>
      }
    </div>
  );
}