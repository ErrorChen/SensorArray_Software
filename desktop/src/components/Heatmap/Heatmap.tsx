import * as echarts from "echarts";
import { useEffect, useMemo, useRef } from "react";

import type { BackendSnapshotPayload } from "../../api/types";
import { cellLabel, selectedCells } from "../../state/appStore";
import { resolveColourRange, type HeatmapDatum } from "../../state/heatmap";

type Props = {
  snapshot: BackendSnapshotPayload | null;
  onSelectCell: (cell: string) => void;
  onSetFreezeColor: (freeze: boolean) => void;
};

export function Heatmap({ snapshot, onSelectCell, onSetFreezeColor }: Props): JSX.Element {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const selected = useMemo(() => selectedCells(snapshot?.selection), [snapshot?.selection]);

  useEffect(() => {
    if (!hostRef.current) {
      return;
    }
    chartRef.current = echarts.init(hostRef.current, undefined, { renderer: "canvas" });
    const observer = new ResizeObserver(() => chartRef.current?.resize());
    observer.observe(hostRef.current);
    chartRef.current.on("click", (params) => {
      const value = params.value as [number, number, number | null] | undefined;
      if (!value) {
        return;
      }
      onSelectCell(cellLabel(value[1], value[0]));
    });
    return () => {
      observer.disconnect();
      chartRef.current?.dispose();
      chartRef.current = null;
    };
  }, [onSelectCell]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !snapshot) {
      return;
    }
    const matrix = snapshot.matrix.displayValues;
    const data: HeatmapDatum[] = [];
    for (let row = 0; row < 8; row += 1) {
      for (let col = 0; col < 8; col += 1) {
        const label = cellLabel(row, col);
        const value = matrix[row]?.[col];
        const valid = Boolean(snapshot.matrix.validMask[row]?.[col]) && typeof value === "number" && Number.isFinite(value);
        data.push([col, row, valid ? value : null, label, valid]);
      }
    }
    const [min, max] = resolveColourRange(data, snapshot);
    chart.setOption(
      {
      animation: false,
      grid: { left: 64, right: 28, top: 28, bottom: 52 },
      tooltip: {
        formatter: (params: echarts.TooltipComponentFormatterCallbackParams) => {
          const item = Array.isArray(params) ? params[0] : params;
          const value = item.value as [number, number, number | null, string, boolean];
          const col = value[0];
          const row = value[1];
          const label = value[3];
          const valid = value[4];
          const corrected = snapshot.matrix.correctedPf[row]?.[col];
          const rawPf = snapshot.matrix.rawPf[row]?.[col];
          const rawFixed = snapshot.matrix.rawFixed[row]?.[col];
          return [
            `<strong>${label}</strong>`,
            `corrected pF: ${formatValue(corrected)}`,
            `raw pF: ${formatValue(rawPf)}`,
            `rawFixed: ${formatValue(rawFixed, 0)}`,
            `seq: ${snapshot.frame.seq ?? "-"}`,
            `valid: ${valid ? "yes" : "no"}`,
            `source: ${snapshot.connection.mode}`
          ].join("<br/>");
        }
      },
      xAxis: {
        type: "category",
        data: snapshot.matrix.cols,
        splitArea: { show: true },
        axisTick: { show: false }
      },
      yAxis: {
        type: "category",
        data: snapshot.matrix.rows,
        inverse: true,
        splitArea: { show: true },
        axisTick: { show: false }
      },
      visualMap: {
        min,
        max,
        dimension: 2,
        calculable: false,
        orient: "horizontal",
        left: "center",
        bottom: 8,
        inRange: { color: ["#1f77b4", "#f7f7f7", "#d62728"] },
        text: [snapshot.matrix.unit, ""]
      },
      series: [
        {
          type: "heatmap",
          data,
          encode: { x: 0, y: 1, value: 2 },
          label: {
            show: snapshot.display.showCellText,
            formatter: (params: { value: [number, number, number | null] }) => formatValue(params.value[2], snapshot.matrix.unit === "%" ? 2 : 2)
          },
          itemStyle: {
            borderColor: "#ffffff",
            borderWidth: 1
          },
          emphasis: {
            itemStyle: {
              borderColor: "#111827",
              borderWidth: 2
            }
          }
        },
        {
          type: "scatter",
          symbolSize: 42,
          data: data.filter((item) => selected.has(item[3])).map((item) => [item[0], item[1]]),
          itemStyle: { color: "transparent", borderColor: "#111827", borderWidth: 3 },
          tooltip: { show: false },
          silent: true
        }
      ]
      },
      { notMerge: false, lazyUpdate: true }
    );
  }, [snapshot, selected]);

  return (
    <section className="heatmapPanel">
      <div className="panelHeader panelHeaderWithActions">
        <span>8x8 Heatmap</span>
        <div className="headerActions">
          <button className={!snapshot?.display.freezeColor ? "active" : ""} onClick={() => onSetFreezeColor(false)}>
            Auto colour
          </button>
          <button className={snapshot?.display.freezeColor ? "active" : ""} onClick={() => onSetFreezeColor(true)}>
            Freeze colour
          </button>
        </div>
      </div>
      <div ref={hostRef} className="heatmapCanvas" />
      {snapshot?.frame.valid ? null : <div className="emptyOverlay">No data yet</div>}
    </section>
  );
}

function formatValue(value: number | null | undefined, digits = 3): string {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "NA";
  }
  return value.toFixed(digits);
}
