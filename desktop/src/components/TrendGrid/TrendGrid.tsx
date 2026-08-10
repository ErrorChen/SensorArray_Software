import * as echarts from "echarts";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import type { BackendHttpClient } from "../../api/httpClient";
import type { BackendSnapshotPayload, HistoryPayload, HistorySeries, MeasurementMode, MeasurementQuantity } from "../../api/types";
import { appliedMeasurementMode, formatMeasurementValue, quantityForMode, quantityLabel } from "../../state/measurement";

type Props = {
  client: BackendHttpClient | null;
  snapshot: BackendSnapshotPayload | null;
  history: HistoryPayload | null;
  onHistory: (history: HistoryPayload) => void;
  onError: (message: string) => void;
};

const trendWindowOptions = [
  { label: "Latest 300", value: 300 },
  { label: "Latest 600", value: 600 },
  { label: "Latest 1200", value: 1200 },
  { label: "Latest 3000", value: 3000 },
  { label: "All session", value: 0 }
];

export function TrendGrid({ client, snapshot, history, onHistory, onError }: Props): JSX.Element {
  const [latestN, setLatestN] = useState(history?.latestN ?? 600);
  const [pending, setPending] = useState(false);
  const appliedMode = appliedMeasurementMode(snapshot);
  const visibleHistory = historyForMode(history, appliedMode);
  const series = visibleHistory?.series ?? [];
  const padded = [0, 1, 2, 3].map((index) => series[index] ?? { cell: "-", points: [] });
  const hasData = series.some((item) => item.points.some((point) => point.value !== null));

  useEffect(() => {
    if (typeof history?.latestN === "number") {
      setLatestN(history.latestN);
    }
  }, [history?.latestN]);

  async function changeTrendWindow(value: number): Promise<void> {
    setLatestN(value);
    if (!client) {
      return;
    }
    setPending(true);
    try {
      onHistory(await client.getHistory(value));
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="trendPanel">
      <div className="panelHeader panelHeaderWithActions">
        <span>
          {visibleHistory?.title ?? "S1 Primary FDC D1-D4"} · {quantityLabel(visibleHistory?.quantity ?? quantityForMode(appliedMode))}
        </span>
        <label className="compactField">
          <span>Trend Window</span>
          <select disabled={pending} value={latestN} onChange={(event) => void changeTrendWindow(Number(event.target.value))}>
            {trendWindowOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="trendGrid">
        {padded.map((item, index) => (
          <TrendChart
            key={`${appliedMode}-${item.cell}-${index}`}
            series={item}
            unit={visibleHistory?.unit ?? unitForMode(appliedMode)}
            quantity={visibleHistory?.quantity ?? quantityForMode(appliedMode)}
          />
        ))}
      </div>
      {hasData ? null : <div className="trendEmpty">No data yet</div>}
    </section>
  );
}

function TrendChart({ series, unit, quantity }: { series: HistorySeries; unit: string; quantity: MeasurementQuantity }): JSX.Element {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);

  useLayoutEffect(() => {
    if (!hostRef.current) {
      return;
    }
    const chart = echarts.init(hostRef.current, undefined, { renderer: "canvas" });
    chartRef.current = chart;
    const observer = new ResizeObserver(() => {
      requestAnimationFrame(() => chartRef.current?.resize());
    });
    observer.observe(hostRef.current);
    return () => {
      observer.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) {
      return;
    }
    const points = buildTrendPoints(series);
    const xValues = points.map((point) => point.value[0]);
    const firstX = xValues.length === 1 ? xValues[0] - 1 : xValues[0] ?? 0;
    const lastX = xValues.length === 1 ? xValues[0] + 1 : xValues[xValues.length - 1] ?? 1;
    chart.setOption(
      {
        animation: false,
        title: { text: series.cell, left: 8, top: 4, textStyle: { fontSize: 12, fontWeight: 600 } },
        grid: { left: 44, right: 16, top: 34, bottom: 30 },
        tooltip: {
          trigger: "axis",
          formatter: (params: unknown) => formatTooltip(params, unit, quantity)
        },
        xAxis: {
          type: "value",
          min: firstX,
          max: lastX,
          scale: true,
          name: points.some((point) => point.timeSeconds !== null) ? "s" : "sample",
          nameTextStyle: { fontSize: 10 },
          splitLine: { show: false }
        },
        yAxis: { type: "value", name: unit === "ohm" ? "Ω" : unit, nameTextStyle: { fontSize: 10 }, scale: true },
        series: [{ type: "line", showSymbol: false, data: points, lineStyle: { width: 1.6, color: "#0f766e" } }]
      },
      { notMerge: true, lazyUpdate: false }
    );
    requestAnimationFrame(() => chart.resize());
  }, [quantity, series, unit]);

  return <div ref={hostRef} className="trendCanvas" />;
}

type TrendDatum = {
  value: [number, number];
  seq: number;
  timeSeconds: number | null;
  cell: string;
};

export function buildTrendPoints(series: HistorySeries): TrendDatum[] {
  const visible = series.points.filter((point) => point.value !== null && point.valid !== false && point.fresh !== false);
  const firstTime = visible.find((point) => typeof point.timeSeconds === "number")?.timeSeconds ?? null;
  return visible.map((point, index) => {
    const x = typeof point.timeSeconds === "number" && firstTime !== null ? point.timeSeconds - firstTime : index;
    return {
      value: [x, point.value ?? Number.NaN],
      seq: point.seq,
      timeSeconds: point.timeSeconds,
      cell: series.cell
    };
  });
}

function formatTooltip(params: unknown, unit: string, quantity: MeasurementQuantity): string {
  const first = Array.isArray(params) ? params[0] : params;
  if (!first || typeof first !== "object" || !("data" in first)) {
    return "";
  }
  const data = (first as { data?: TrendDatum }).data;
  if (!data) {
    return "";
  }
  return [
    `<strong>${data.cell}</strong>`,
    `value: ${formatMeasurementValue(data.value[1], quantity, { percent: unit === "%" })}`,
    `seq: ${data.seq}`,
    `time: ${typeof data.timeSeconds === "number" ? data.timeSeconds.toFixed(3) : "NA"}`
  ].join("<br/>");
}

export function historyForMode(history: HistoryPayload | null, mode: MeasurementMode): HistoryPayload | null {
  if (!history) {
    return null;
  }
  const historyMode = history.mode ?? modeFromQuantity(history.quantity);
  return historyMode === mode ? history : null;
}

function modeFromQuantity(quantity: MeasurementQuantity | undefined): MeasurementMode {
  if (quantity === "voltage") {
    return "VOLT";
  }
  if (quantity === "resistance") {
    return "RES";
  }
  return "CAP";
}

function unitForMode(mode: MeasurementMode): string {
  return mode === "VOLT" ? "V" : mode === "RES" ? "ohm" : "pF";
}
