const SEV_STYLE = {
  CRITICAL: { bg: "#2d0a0a", border: "#c0392b", badge: "#e74c3c" },
  WARN:     { bg: "#2d1a00", border: "#c07000", badge: "#f39c12" },
  INFO:     { bg: "#0a1e2d", border: "#1a6090", badge: "#3498db" },
};

export default function AnomalyFeed({ anomalies }) {
  const list = anomalies || [];
  return (
    <div style={{
      background: "#1e1e2e", borderRadius: 12, padding: "20px 24px",
      boxShadow: "0 2px 8px rgba(0,0,0,0.4)",
    }}>
      <div style={{ display: "flex", justifyContent: "space-between",
                    alignItems: "center", marginBottom: 16 }}>
        <h3 style={{ margin: 0, fontSize: 15, fontWeight: 600, color: "#e2e8f0" }}>
          Active anomalies
        </h3>
        {list.length > 0 &&
          <span style={{ fontSize: 12, background: "#3d0000", color: "#ef4444",
                         padding: "2px 10px", borderRadius: 99, fontWeight: 600 }}>
            {list.length} active
          </span>
        }
      </div>

      {list.length === 0
        ? <div style={{ display: "flex", alignItems: "center", gap: 8,
                        color: "#22c55e", fontSize: 14 }}>
            <span>✓</span><span>No active anomalies</span>
          </div>
        : <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {list.map((a) => {
              const s = SEV_STYLE[a.severity] || SEV_STYLE.INFO;
              return (
                <div key={a.anomaly_id} style={{
                  background: s.bg, border: `1px solid ${s.border}`,
                  borderRadius: 8, padding: "12px 16px",
                  display: "flex", alignItems: "flex-start", gap: 12,
                }}>
                  <span style={{
                    fontSize: 11, fontWeight: 700, color: s.badge,
                    border: `1px solid ${s.border}`,
                    padding: "2px 8px", borderRadius: 4,
                    whiteSpace: "nowrap", marginTop: 1,
                  }}>
                    {a.severity}
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
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