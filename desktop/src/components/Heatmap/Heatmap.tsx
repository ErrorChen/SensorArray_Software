import * as echarts from "echarts";
import { useEffect, useLayoutEffect, useRef } from "react";
import type { MutableRefObject } from "react";

import type { BackendSnapshotPayload } from "../../api/types";
import { cellLabel, selectedCells } from "../../state/appStore";
import { resolveColourRange, type HeatmapDatum } from "../../state/heatmap";

type Props = {
  snapshot: BackendSnapshotPayload | null;
  onSelectCell: (cell: string) => void;
  onSetFreezeColor: (freeze: boolean) => void;
};

type CellPoint = { row: number; col: number };

export function Heatmap({ snapshot, onSelectCell, onSetFreezeColor }: Props): JSX.Element {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const onSelectCellRef = useRef(onSelectCell);
  const latestSnapshotRef = useRef<BackendSnapshotPayload | null>(null);
  const latestSelectedRef = useRef<Set<string>>(new Set());
  const rafIdRef = useRef<number | null>(null);
  const pendingDynamicOptionRef = useRef<echarts.EChartsOption | null>(null);

  useEffect(() => {
    onSelectCellRef.current = onSelectCell;
  }, [onSelectCell]);

  useEffect(() => {
    latestSnapshotRef.current = snapshot;
    latestSelectedRef.current = selectedCells(snapshot?.selection);
  }, [snapshot]);

  useLayoutEffect(() => {
    if (!hostRef.current) {
      return;
    }
    const chart = initialiseHeatmapChart(hostRef.current);
    chartRef.current = chart;
    chart.setOption(buildStaticHeatmapOption(latestSnapshotRef), { notMerge: true, lazyUpdate: false });

    const removeHandlers = installHeatmapPointerHandlers(chart, onSelectCellRef);
    const observer = new ResizeObserver(() => {
      requestAnimationFrame(() => chartRef.current?.resize());
    });
    observer.observe(hostRef.current);

    return () => {
      removeHandlers();
      observer.disconnect();
      if (rafIdRef.current !== null) {
        cancelAnimationFrame(rafIdRef.current);
        rafIdRef.current = null;
      }
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !snapshot) {
      return;
    }
    pendingDynamicOptionRef.current = buildDynamicHeatmapOption(snapshot, latestSelectedRef.current);
    if (rafIdRef.current !== null) {
      return;
    }
    rafIdRef.current = requestAnimationFrame(() => {
      rafIdRef.current = null;
      const pending = pendingDynamicOptionRef.current;
      pendingDynamicOptionRef.current = null;
      if (!pending || !chartRef.current) {
        return;
      }
      chartRef.current.setOption(pending, { notMerge: false, lazyUpdate: true });
    });
  }, [snapshot]);

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

function initialiseHeatmapChart(host: HTMLDivElement): echarts.ECharts {
  return echarts.init(host, undefined, { renderer: "canvas" });
}

function buildStaticHeatmapOption(snapshotRef: MutableRefObject<BackendSnapshotPayload | null>): echarts.EChartsOption {
  return {
    animation: false,
    grid: { left: 64, right: 28, top: 28, bottom: 52 },
    tooltip: {
      trigger: "item",
      confine: true,
      transitionDuration: 0,
      showDelay: 0,
      hideDelay: 80,
      formatter: (params: echarts.TooltipComponentFormatterCallbackParams) => formatHeatmapTooltip(params, snapshotRef.current)
    },
    xAxis: {
      type: "category",
      data: Array.from({ length: 8 }, (_, index) => `D${index + 1}`),
      splitArea: { show: true },
      axisTick: { show: false }
    },
    yAxis: {
      type: "category",
      data: Array.from({ length: 8 }, (_, index) => `S${index + 1}`),
      inverse: true,
      splitArea: { show: true },
      axisTick: { show: false }
    },
    visualMap: {
      min: 0,
      max: 1,
      dimension: 2,
      calculable: false,
      orient: "horizontal",
      left: "center",
      bottom: 8,
      inRange: { color: ["#1f77b4", "#f7f7f7", "#d62728"] },
      text: ["pF", ""]
    },
    series: [
      {
        id: "heatmap-values",
        type: "heatmap",
        data: [],
        cursor: "pointer",
        encode: { x: 0, y: 1, value: 2 },
        itemStyle: { borderColor: "#ffffff", borderWidth: 1 },
        emphasis: { itemStyle: { borderColor: "#111827", borderWidth: 2 } }
      },
      {
        id: "selected-cells",
        type: "scatter",
        symbolSize: 42,
        data: [],
        itemStyle: { color: "transparent", borderColor: "#111827", borderWidth: 3 },
        tooltip: { show: false },
        silent: true
      }
    ]
  };
}

export function buildHeatmapData(snapshot: BackendSnapshotPayload): HeatmapDatum[] {
  const data: HeatmapDatum[] = [];
  for (let row = 0; row < 8; row += 1) {
    for (let col = 0; col < 8; col += 1) {
      const label = cellLabel(row, col);
      const value = snapshot.matrix.displayValues[row]?.[col];
      const valid = Boolean(snapshot.matrix.validMask[row]?.[col]) && typeof value === "number" && Number.isFinite(value);
      data.push([col, row, valid ? value : null, label, valid]);
    }
  }
  return data;
}

export function buildSelectedCellData(data: HeatmapDatum[], selected: Set<string>): number[][] {
  return data.filter((item) => selected.has(item[3])).map((item) => [item[0], item[1]]);
}

export function buildDynamicHeatmapOption(snapshot: BackendSnapshotPayload, selected: Set<string>): echarts.EChartsOption {
  const data = buildHeatmapData(snapshot);
  const [min, max] = resolveColourRange(data, snapshot);
  return {
    xAxis: { data: snapshot.matrix.cols },
    yAxis: { data: snapshot.matrix.rows },
    visualMap: {
      min,
      max,
      text: [snapshot.matrix.unit, ""]
    },
    series: [
      {
        id: "heatmap-values",
        type: "heatmap",
        data: data.map((item) => [item[0], item[1], item[2], item[3], item[4] ? 1 : 0]),
        label: {
          show: snapshot.display.showCellText,
          formatter: (params: { value?: unknown }) => {
            const value = params.value as [number, number, number | null] | undefined;
            return formatValue(value?.[2], snapshot.matrix.unit === "%" ? 2 : 2);
          }
        }
      },
      {
        id: "selected-cells",
        type: "scatter",
        data: buildSelectedCellData(data, selected),
        silent: true
      }
    ]
  };
}

export function formatHeatmapTooltip(params: echarts.TooltipComponentFormatterCallbackParams, snapshot: BackendSnapshotPayload | null): string {
  const item = Array.isArray(params) ? params[0] : params;
  const value = item?.value as [number, number, number | null, string, number] | undefined;
  if (!value || !snapshot) {
    return "No data";
  }
  const col = value[0];
  const row = value[1];
  const label = value[3];
  const valid = value[4] === 1;
  const corrected = snapshot.matrix.correctedPf[row]?.[col];
  const rawPf = snapshot.matrix.rawPf[row]?.[col];
  const rawFixed = snapshot.matrix.rawFixed[row]?.[col];
  const userOffset = snapshot.matrix.userOffsetPf[row]?.[col];
  const displayed = snapshot.matrix.displayValues[row]?.[col];
  return [
    `<strong>${label}</strong>`,
    `raw pF: ${formatValue(rawPf)}`,
    `corrected pF: ${formatValue(corrected)}`,
    `user offset pF: ${formatValue(userOffset)}`,
    `displayed ${snapshot.matrix.unit}: ${formatValue(displayed, snapshot.matrix.unit === "%" ? 2 : 3)}`,
    `rawFixed: ${formatValue(rawFixed, 0)}`,
    `seq: ${snapshot.frame.seq ?? "-"}`,
    `valid: ${valid ? "yes" : "no"}`,
    `source: ${snapshot.connection.mode}`
  ].join("<br/>");
}

export function pointToCell(chart: echarts.ECharts, offsetX: number, offsetY: number): CellPoint | null {
  const pixel: [number, number] = [offsetX, offsetY];
  if (!chart.containPixel({ gridIndex: 0 }, pixel)) {
    return null;
  }
  const coord = chart.convertFromPixel({ gridIndex: 0 }, pixel);
  if (!Array.isArray(coord) || coord.length < 2) {
    return null;
  }
  const col = Math.round(Number(coord[0]));
  const row = Math.round(Number(coord[1]));
  if (!Number.isFinite(row) || !Number.isFinite(col)) {
    return null;
  }
  if (row < 0 || row >= 8 || col < 0 || col >= 8) {
    return null;
  }
  return { row, col };
}

function installHeatmapPointerHandlers(
  chart: echarts.ECharts,
  selectCellRef: MutableRefObject<(cell: string) => void>
): () => void {
  const handlePointerDown = (event: { offsetX: number; offsetY: number }) => {
    const point = pointToCell(chart, event.offsetX, event.offsetY);
    if (point) {
      selectCellRef.current(cellLabel(point.row, point.col));
    }
  };
  const handleMouseMove = (event: { offsetX: number; offsetY: number }) => {
    chart.getZr().setCursorStyle(pointToCell(chart, event.offsetX, event.offsetY) ? "pointer" : "default");
  };
  chart.getZr().on("mousedown", handlePointerDown);
  chart.getZr().on("mousemove", handleMouseMove);
  return () => {
    chart.getZr().off("mousedown", handlePointerDown);
    chart.getZr().off("mousemove", handleMouseMove);
  };
}

function formatValue(value: number | null | undefined, digits = 3): string {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "NA";
  }
  return value.toFixed(digits);
}
