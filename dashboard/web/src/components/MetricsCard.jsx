export default function MetricsCard({ label, value, color, sub }) {
  return (
    <div style={{
      background: "#1e1e2e", borderRadius: 12, padding: "20px 24px",
      borderLeft: `4px solid ${color}`,
      boxShadow: "0 2px 8px rgba(0,0,0,0.4)",
    }}>
      <div style={{ fontSize: 28, fontWeight: 700, color, lineHeight: 1 }}>
        {value}
      </div>
      <div style={{ fontSize: 13, color: "#64748b", marginTop: 6 }}>{label}</div>
      {sub && (
        <div style={{ fontSize: 11, color: "#f59e0b", marginTop: 4 }}>{sub}</div>
      )}
    </div>
  );
}