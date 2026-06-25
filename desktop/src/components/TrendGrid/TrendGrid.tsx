import * as echarts from "echarts";
import { useEffect, useRef } from "react";

import type { HistoryPayload, HistorySeries } from "../../api/types";

type Props = {
  history: HistoryPayload | null;
};

export function TrendGrid({ history }: Props): JSX.Element {
  const series = history?.series ?? [];
  const padded = [0, 1, 2, 3].map((index) => series[index] ?? { cell: "-", points: [] });
  return (
    <section className="trendPanel">
      <div className="panelHeader">{history?.title ?? "S1 · Primary FDC · D1-D4"}</div>
      <div className="trendGrid">
        {padded.map((item, index) => (
          <TrendChart key={`${item.cell}-${index}`} series={item} unit={history?.unit ?? "pF"} />
        ))}
      </div>
    </section>
  );
}

function TrendChart({ series, unit }: { series: HistorySeries; unit: string }): JSX.Element {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!hostRef.current) {
      return;
    }
    chartRef.current = echarts.init(hostRef.current, undefined, { renderer: "canvas" });
    const resize = () => chartRef.current?.resize();
    window.addEventListener("resize", resize);
    return () => {
      window.removeEventListener("resize", resize);
      chartRef.current?.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) {
      return;
    }
    const points = series.points.filter((point) => point.value !== null).map((point) => [point.seq, point.value]);
    chart.setOption({
      animation: false,
      title: { text: series.cell, left: 8, top: 4, textStyle: { fontSize: 12, fontWeight: 600 } },
      grid: { left: 42, right: 16, top: 34, bottom: 28 },
      tooltip: { trigger: "axis" },
      xAxis: { type: "value", name: "seq", nameTextStyle: { fontSize: 10 }, splitLine: { show: false } },
      yAxis: { type: "value", name: unit, nameTextStyle: { fontSize: 10 }, scale: true },
      series: [{ type: "line", showSymbol: false, data: points, lineStyle: { width: 1.6, color: "#0f766e" } }]
    });
  }, [series, unit]);

  return <div ref={hostRef} className="trendCanvas" />;
}
