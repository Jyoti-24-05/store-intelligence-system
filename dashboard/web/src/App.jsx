import { useState, useEffect, useCallback } from "react";
import MetricsCard  from "./components/MetricsCard";
import FunnelChart  from "./components/FunnelChart";
import Heatmap      from "./components/Heatmap";
import AnomalyFeed  from "./components/AnomalyFeed";

const API_URL  = import.meta.env.VITE_API_URL  || "http://localhost:8000";
const STORE_ID = import.meta.env.VITE_STORE_ID || "STORE_BLR_002";

export default function App() {
  const [metrics,   setMetrics]   = useState(null);
  const [anomalies, setAnomalies] = useState([]);
  const [funnel,    setFunnel]    = useState(null);
  const [heatmap,   setHeatmap]   = useState(null);
  const [connected, setConnected] = useState(false);
  const [lastUpdate, setLastUpdate] = useState(null);

  // SSE real-time stream for metrics + anomalies + funnel
  useEffect(() => {
    const es = new EventSource(`${API_URL}/stores/${STORE_ID}/stream`);
    es.onopen    = () => setConnected(true);
    es.onerror   = () => setConnected(false);
    es.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data);
        if (d.error) return;
        setMetrics(d.metrics);
        setAnomalies(d.anomalies || []);
        setFunnel(d.funnel);
        setLastUpdate(new Date());
      } catch {}
    };
    return () => es.close();
  }, []);

  // Heatmap polled separately (changes slowly)
  const fetchHeatmap = useCallback(async () => {
    try {
      const r = await fetch(`${API_URL}/stores/${STORE_ID}/heatmap`);
      setHeatmap(await r.json());
    } catch {}
  }, []);

  useEffect(() => {
    fetchHeatmap();
    const id = setInterval(fetchHeatmap, 15000);
    return () => clearInterval(id);
  }, [fetchHeatmap]);

  return (
    <div style={{ minHeight: "100vh", background: "#0f0f14", color: "#e2e8f0",
                  fontFamily: "'Inter', sans-serif", padding: "24px" }}>

      {/* Header */}
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between",
                    marginBottom: 24 }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 22, fontWeight: 700, color: "#a78bfa" }}>
            Store Intelligence
          </h1>
          <p style={{ margin: "4px 0 0", fontSize: 13, color: "#64748b" }}>
            {STORE_ID} — {lastUpdate ? `Updated ${lastUpdate.toLocaleTimeString()}` : "Connecting…"}
          </p>
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{ width: 8, height: 8, borderRadius: "50%",
                        background: connected ? "#22c55e" : "#ef4444" }} />
          <span style={{ fontSize: 13, color: connected ? "#22c55e" : "#ef4444" }}>
            {connected ? "Live" : "Disconnected"}
          </span>
        </div>
      </div>

      {/* Metrics row */}
      <div style={{ display: "grid", gridTemplateColumns: "repeat(4, 1fr)", gap: 16,
                    marginBottom: 24 }}>
        <MetricsCard label="Unique Visitors"  value={metrics?.unique_visitors ?? "—"}
                     icon="👥" color="#818cf8" />
        <MetricsCard label="Conversion Rate"
                     value={metrics ? `${(metrics.conversion_rate * 100).toFixed(1)}%` : "—"}
                     icon="💳" color="#34d399" />
        <MetricsCard label="Queue Depth"      value={metrics?.current_queue_depth ?? "—"}
                     icon="🧾"
                     color={!metrics ? "#94a3b8"
                           : metrics.current_queue_depth < 3  ? "#22c55e"
                           : metrics.current_queue_depth <= 6 ? "#f59e0b"
                           :                                    "#ef4444"} />
        <MetricsCard label="Abandonment Rate"
                     value={metrics ? `${(metrics.abandonment_rate * 100).toFixed(1)}%` : "—"}
                     icon="🚶" color="#f87171" />
      </div>

      {/* Funnel + Heatmap row */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16,
                    marginBottom: 24 }}>
        <FunnelChart funnel={funnel} />
        <Heatmap heatmap={heatmap} />
      </div>

      {/* Anomaly feed */}
      <AnomalyFeed anomalies={anomalies} />
    </div>
  );
}