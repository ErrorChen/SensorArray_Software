import * as echarts from "echarts";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { MutableRefObject } from "react";

import type { BackendSnapshotPayload } from "../../api/types";
import { cellLabel, selectedCells } from "../../state/appStore";
import { resolveColourRange, type HeatmapDatum } from "../../state/heatmap";
import {
  appliedMeasurementMode,
  cellMeasurementState,
  formatErrorCode,
  formatMeasurementValue,
  isCellDisplayable,
  matrixDisplayUnit,
  measurementQuantity,
  pgaLabel,
  quantityLabel,
  rawFixedLabel
} from "../../state/measurement";

type Props = {
  snapshot: BackendSnapshotPayload | null;
  onSelectCell: (cell: string) => void;
  onSetFreezeColor: (freeze: boolean) => void;
};

type CellPoint = { row: number; col: number };
type InvalidHover = CellPoint & { left: number; top: number };

export function Heatmap({ snapshot, onSelectCell, onSetFreezeColor }: Props): JSX.Element {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const onSelectCellRef = useRef(onSelectCell);
  const latestSnapshotRef = useRef<BackendSnapshotPayload | null>(null);
  const latestSelectedRef = useRef<Set<string>>(new Set());
  const rafIdRef = useRef<number | null>(null);
  const pendingDynamicOptionRef = useRef<echarts.EChartsOption | null>(null);
  const [invalidHover, setInvalidHover] = useState<InvalidHover | null>(null);

  useEffect(() => {
    onSelectCellRef.current = onSelectCell;
  }, [onSelectCell]);

  useEffect(() => {
    latestSnapshotRef.current = snapshot;
    latestSelectedRef.current = selectedCells(snapshot?.selection);
  }, [snapshot]);

  function showInvalidHover(row: number, col: number): void {
    const host = hostRef.current;
    if (!host || !snapshot?.frame.valid) {
      setInvalidHover(null);
      return;
    }
    const gridWidth = host.clientWidth - 64 - 28;
    const gridHeight = host.clientHeight - 28 - 52;
    const offsetX = 64 + ((col + 0.5) * gridWidth) / 8;
    const offsetY = 28 + ((row + 0.5) * gridHeight) / 8;
    const canvasTop = host.parentElement?.offsetTop ?? 0;
    const left = Math.max(8, Math.min(host.clientWidth - 340, offsetX + 12));
    const top = canvasTop + Math.max(8, Math.min(host.clientHeight - 190, offsetY - 16));
    setInvalidHover({ row, col, left, top });
  }

  const invalidTargets = snapshot ? invalidCellTargets(snapshot) : [];

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
      // visualMap component text is not reliably updated by ECharts' ordinary
      // positional array merge. Replacing that component prevents a previous
      // quantity label (for example pF) surviving a CAP -> VOLT/RES switch.
      chartRef.current.setOption(pending, { notMerge: false, lazyUpdate: true, replaceMerge: ["visualMap"] });
    });
  }, [snapshot]);

  return (
    <section className="heatmapPanel">
      <div className="panelHeader panelHeaderWithActions">
        <span>
          8x8 {snapshot ? quantityLabel(measurementQuantity(snapshot)) : "Measurement"} Heatmap
        </span>
        <div className="headerActions">
          <button className={!snapshot?.display.freezeColor ? "active" : ""} onClick={() => onSetFreezeColor(false)}>
            Auto colour
          </button>
          <button className={snapshot?.display.freezeColor ? "active" : ""} onClick={() => onSetFreezeColor(true)}>
            Freeze colour
          </button>
        </div>
      </div>
      <div className="heatmapCanvasLayer">
        <div
          ref={hostRef}
          className="heatmapCanvas"
          aria-label={`Measurement heatmap; colour scale unit ${snapshot ? matrixDisplayUnit(snapshot) : "unknown"}`}
        />
        <div className="invalidCellHitLayer" aria-label="Invalid and stale measurement cells">
          {invalidTargets.map(({ row, col, label }) => (
            <button
              key={label}
              type="button"
              className="invalidCellHitTarget"
              style={{ gridColumn: col + 1, gridRow: row + 1 }}
              aria-label={`${label} measurement diagnostics`}
              onMouseEnter={() => showInvalidHover(row, col)}
              onMouseLeave={() => setInvalidHover(null)}
              onFocus={() => showInvalidHover(row, col)}
              onBlur={() => setInvalidHover(null)}
              onClick={() => onSelectCellRef.current(label)}
            />
          ))}
        </div>
      </div>
      {invalidHover && snapshot ? (
        <div
          className="heatmapInvalidTooltip"
          data-testid="heatmap-invalid-tooltip"
          style={{ left: invalidHover.left, top: invalidHover.top }}
          dangerouslySetInnerHTML={{
            __html: formatHeatmapTooltip(
              {
                value: [
                  invalidHover.col,
                  invalidHover.row,
                  0,
                  cellLabel(invalidHover.row, invalidHover.col),
                  0
                ]
              } as never,
              snapshot
            )
          }}
        />
      ) : null}
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
    visualMap: buildVisualMapOptions(0, 1, "pF"),
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
        id: "invalid-cells",
        type: "heatmap",
        data: [],
        cursor: "pointer",
        encode: { x: 0, y: 1, value: 2 },
        itemStyle: { color: "#e5e7eb", borderColor: "#ffffff", borderWidth: 1 }
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
      const cell = cellMeasurementState(snapshot.matrix, row, col);
      const displayable = isCellDisplayable(cell);
      data.push([col, row, displayable ? cell.value : null, label, displayable]);
    }
  }
  return data;
}

export function invalidCellTargets(snapshot: BackendSnapshotPayload): Array<CellPoint & { label: string }> {
  if (!snapshot.frame.valid) {
    return [];
  }
  const targets: Array<CellPoint & { label: string }> = [];
  for (let row = 0; row < snapshot.frame.rows; row += 1) {
    for (let col = 0; col < 8; col += 1) {
      if (!isCellDisplayable(cellMeasurementState(snapshot.matrix, row, col))) {
        targets.push({ row, col, label: cellLabel(row, col) });
      }
    }
  }
  return targets;
}

export function buildSelectedCellData(data: HeatmapDatum[], selected: Set<string>): number[][] {
  return data.filter((item) => selected.has(item[3])).map((item) => [item[0], item[1]]);
}

export function buildDynamicHeatmapOption(snapshot: BackendSnapshotPayload, selected: Set<string>): echarts.EChartsOption {
  const data = buildHeatmapData(snapshot);
  const [min, max] = resolveColourRange(data, snapshot);
  const cellLabelOption = {
    show: snapshot.display.showCellText,
    formatter: (params: { value?: unknown }) => {
      const value = params.value as [number, number, number | null] | undefined;
      return value ? formatHeatmapCellLabel(snapshot, value[1], value[0], value[2]) : "";
    }
  };
  return {
    xAxis: { data: snapshot.matrix.cols },
    yAxis: { data: snapshot.matrix.rows },
    visualMap: buildVisualMapOptions(min, max, matrixDisplayUnit(snapshot)),
    series: [
      {
        id: "heatmap-values",
        type: "heatmap",
        data: data.filter((item) => item[4]).map((item) => ({
          value: [item[0], item[1], item[2], item[3], item[4] ? 1 : 0],
          itemStyle: undefined
        })),
        label: cellLabelOption,
        tooltip: {
          show: true,
          formatter: (params: echarts.TooltipComponentFormatterCallbackParams) => formatHeatmapTooltip(params, snapshot)
        }
      },
      {
        id: "invalid-cells",
        type: "heatmap",
        data: data
          .filter((item) => snapshot.frame.valid && item[1] < snapshot.frame.rows && !item[4])
          .map((item) => ({
            // A finite plotting placeholder makes an active invalid/stale cell
            // addressable for tooltip/click handling. Inactive rows remain
            // empty grid cells, and visualMap excludes this grey series.
            value: [item[0], item[1], 0, item[3], 0]
          })),
        label: cellLabelOption,
        tooltip: {
          show: true,
          formatter: (params: echarts.TooltipComponentFormatterCallbackParams) => formatHeatmapTooltip(params, snapshot)
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

export function buildVisualMapOptions(
  min: number,
  max: number,
  unit: string
): NonNullable<echarts.EChartsOption["visualMap"]> {
  return [
    {
      id: "measurement-scale",
      min,
      max,
      dimension: 2,
      // Invalid/stale cells live in a separate grey series. Restricting the
      // visible map to the valid series keeps its physical domain clean.
      seriesIndex: [0],
      calculable: false,
      orient: "horizontal",
      left: "center",
      bottom: 8,
      inRange: { color: ["#1f77b4", "#f7f7f7", "#d62728"] },
      text: [unit, ""]
    },
    {
      id: "invalid-scale",
      show: false,
      min: 0,
      max: 1,
      dimension: 2,
      seriesIndex: [1],
      inRange: { color: ["#e5e7eb"] }
    }
  ];
}

export function formatHeatmapCellLabel(
  snapshot: BackendSnapshotPayload,
  row: number,
  col: number,
  plottedValue: number | null
): string {
  if (!snapshot.frame.valid || row < 0 || row >= snapshot.frame.rows) {
    return "";
  }
  const cell = cellMeasurementState(snapshot.matrix, row, col);
  // Xhh is the firmware's primary invalid-cell evidence.  It must remain
  // visible even when that conversion is also stale.
  if (!cell.valid) {
    return cell.errorCode === null ? "invalid" : `X${cell.errorCode.toString(16).toUpperCase().padStart(2, "0")}`;
  }
  if (!cell.fresh) {
    return "stale";
  }
  return formatMeasurementValue(plottedValue, measurementQuantity(snapshot), {
    compact: true,
    percent: matrixDisplayUnit(snapshot) === "%"
  });
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
  const mode = appliedMeasurementMode(snapshot);
  const quantity = measurementQuantity(snapshot);
  const displayUnit = matrixDisplayUnit(snapshot);
  const cell = cellMeasurementState(snapshot.matrix, row, col);
  const corrected = snapshot.matrix.correctedPf[row]?.[col];
  const rawPf = snapshot.matrix.rawPf[row]?.[col];
  const userOffset = snapshot.matrix.userOffsetPf[row]?.[col];
  const physical = snapshot.matrix.values?.[row]?.[col] ?? cell.value;
  const lines = [
    `<strong>${escapeHtml(label)}</strong>`,
    `Mode: ${mode}`,
    `Value: ${formatMeasurementValue(cell.value, quantity, { percent: displayUnit === "%" })}`
  ];
  if (mode === "CAP") {
    lines.push(
      `Raw pF: ${formatValue(rawPf)}`,
      `Corrected pF: ${formatValue(corrected)}`,
      `User offset pF: ${formatValue(userOffset)}`
    );
  } else {
    lines.push(
      `${rawFixedLabel(mode)}: ${formatValue(cell.rawFixed, 0)}`,
      `Physical ${quantity}: ${formatMeasurementValue(physical, quantity)}`,
      pgaLabel(cell.pga, cell.pgaBypass)
    );
  }
  lines.push(
    `Unit: ${escapeHtml(displayUnit)}`,
    `Valid: ${cell.valid ? "yes" : "no"}`,
    `Fresh: ${cell.fresh ? "yes" : "no"}`,
    `Error: ${formatErrorCode(cell.errorCode)}${cell.errorReason ? ` — ${escapeHtml(cell.errorReason)}` : ""}`,
    "<span class=\"tooltipDiagnostics\">Frame diagnostics</span>",
    `Seq: ${snapshot.frame.seq ?? "-"}`,
    `Generation: ${snapshot.matrix.generation ?? snapshot.measurement?.generation ?? "-"}`,
    `Request ID: ${snapshot.matrix.requestId ?? snapshot.measurement?.requestId ?? "-"}`,
    `Source: ${escapeHtml(snapshot.matrix.sourceTransport || snapshot.connection.mode)}`
  );
  lines.push(...diagnosticTooltipLines(snapshot));
  return lines.join("<br/>");
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
    const point = pointToCell(chart, event.offsetX, event.offsetY);
    chart.getZr().setCursorStyle(point ? "pointer" : "default");
    if (point) {
      showHeatmapTooltipForCell(chart, point);
    } else {
      chart.dispatchAction({ type: "hideTip" });
    }
  };
  chart.getZr().on("mousedown", handlePointerDown);
  chart.getZr().on("mousemove", handleMouseMove);
  return () => {
    chart.getZr().off("mousedown", handlePointerDown);
    chart.getZr().off("mousemove", handleMouseMove);
  };
}

export function showHeatmapTooltipForCell(chart: echarts.ECharts, point: CellPoint): boolean {
  const configuredSeries = chart.getOption().series;
  const series = Array.isArray(configuredSeries) ? configuredSeries : configuredSeries ? [configuredSeries] : [];
  // Only search the valid and invalid heatmap series. The selected-cell
  // scatter overlay is deliberately silent, but it must not prevent an Xhh or
  // stale cell underneath from exposing its diagnostics in Electron.
  for (let seriesIndex = 0; seriesIndex < Math.min(2, series.length); seriesIndex += 1) {
    const data = (series[seriesIndex] as { data?: unknown[] }).data ?? [];
    const dataIndex = data.findIndex((entry) => {
      const value = entry && typeof entry === "object" && "value" in entry ? (entry as { value: unknown }).value : entry;
      return Array.isArray(value) && Number(value[0]) === point.col && Number(value[1]) === point.row;
    });
    if (dataIndex >= 0) {
      chart.dispatchAction({ type: "showTip", seriesIndex, dataIndex });
      return true;
    }
  }
  chart.dispatchAction({ type: "hideTip" });
  return false;
}

function formatValue(value: number | null | undefined, digits = 3): string {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "NA";
  }
  return value.toFixed(digits);
}

function diagnosticTooltipLines(snapshot: BackendSnapshotPayload): string[] {
  const diagnostics = snapshot.matrix.diagnostics ?? {};
  const lines: string[] = [];
  if (typeof diagnostics.avddUv === "number") {
    lines.push(`AVDD: ${formatMeasurementValue(diagnostics.avddUv * 1e-6, "voltage")}`);
  }
  if (typeof diagnostics.avssUv === "number") {
    lines.push(`AVSS: ${formatMeasurementValue(diagnostics.avssUv * 1e-6, "voltage")}`);
  }
  if (typeof diagnostics.matrixReferenceUv === "number") {
    lines.push(`Matrix reference: ${formatMeasurementValue(diagnostics.matrixReferenceUv * 1e-6, "voltage")}`);
  }
  if (diagnostics.reference !== undefined && diagnostics.reference !== null) {
    lines.push(`Reference: ${escapeHtml(String(diagnostics.reference))}`);
  }
  if (diagnostics.railValid !== undefined && diagnostics.railValid !== null) {
    lines.push(`Rail valid: ${diagnostics.railValid ? "yes" : "no"}`);
  }
  if (diagnostics.railAgeFrames !== undefined && diagnostics.railAgeFrames !== null) {
    lines.push(`Rail age: ${escapeHtml(String(diagnostics.railAgeFrames))} frames`);
  }
  const recoveredRetryCount = diagnostics.recoveredRetryCount ?? diagnostics.ir;
  if (recoveredRetryCount !== undefined && recoveredRetryCount !== null) {
    lines.push(`Recovered I/O retries: ${escapeHtml(String(recoveredRetryCount))}`);
  }
  return lines;
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => {
    const replacements: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
    return replacements[character];
  });
}
