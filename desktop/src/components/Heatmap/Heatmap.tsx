import * as echarts from "echarts";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, MutableRefObject } from "react";

import type { BackendSnapshotPayload, ColourDomain, MeasurementMode } from "../../api/types";
import { cellLabel, selectedCells } from "../../state/appStore";
import { colourDomainForMode, resolveColourRange, type HeatmapDatum } from "../../state/heatmap";
import {
  cellMeasurementState,
  formatErrorCode,
  formatMeasurementValue,
  isCellDisplayable,
  modeForRow,
  pgaLabel,
  quantityForMode,
  quantityForRow,
  quantityLabel,
  rawFixedLabel,
  unitForRow
} from "../../state/measurement";

type Props = {
  snapshot: BackendSnapshotPayload | null;
  onSelectCell: (cell: string) => void;
  onSetFreezeColor: (freeze: boolean) => void;
};

type CellPoint = { row: number; col: number };
type InvalidHover = CellPoint & { left: number; top: number };

const modeOrder: MeasurementMode[] = ["CAP", "VOLT", "RES"];

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
    const rows = activeRows(snapshot);
    const gridWidth = host.clientWidth - 64 - 28;
    const gridHeight = host.clientHeight - 28 - 72;
    const offsetX = 64 + ((col + 0.5) * gridWidth) / 8;
    const offsetY = 28 + ((row + 0.5) * gridHeight) / rows;
    const canvasTop = host.parentElement?.offsetTop ?? 0;
    const left = Math.max(8, Math.min(host.clientWidth - 340, offsetX + 12));
    const top = canvasTop + Math.max(8, Math.min(host.clientHeight - 190, offsetY - 16));
    setInvalidHover({ row, col, left, top });
  }

  const invalidTargets = snapshot ? invalidCellTargets(snapshot) : [];
  const rows = snapshot ? activeRows(snapshot) : 8;

  useLayoutEffect(() => {
    if (!hostRef.current) {
      return;
    }
    const chart = initialiseHeatmapChart(hostRef.current);
    chartRef.current = chart;
    chart.setOption(buildStaticHeatmapOption(latestSnapshotRef), { notMerge: true, lazyUpdate: false });

    const removeHandlers = installHeatmapPointerHandlers(chart, onSelectCellRef, latestSnapshotRef);
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
      chartRef.current.setOption(pending, {
        notMerge: false,
        lazyUpdate: true,
        replaceMerge: ["visualMap", "series"]
      });
    });
  }, [snapshot]);

  function handleKeyboard(event: ReactKeyboardEvent<HTMLDivElement>): void {
    if (!snapshot || !["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(event.key)) {
      return;
    }
    event.preventDefault();
    const current = firstSelectedPoint(snapshot.selection.cells, rows) ?? { row: 0, col: 0 };
    const next = { ...current };
    if (event.key === "ArrowUp") next.row = Math.max(0, current.row - 1);
    if (event.key === "ArrowDown") next.row = Math.min(rows - 1, current.row + 1);
    if (event.key === "ArrowLeft") next.col = Math.max(0, current.col - 1);
    if (event.key === "ArrowRight") next.col = Math.min(7, current.col + 1);
    onSelectCellRef.current(cellLabel(next.row, next.col));
  }

  return (
    <section className="heatmapPanel">
      <div className="panelHeader panelHeaderWithActions">
        <span>{snapshot ? heatmapTitle(snapshot) : "8x8 Measurement Heatmap"}</span>
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
          role="grid"
          tabIndex={0}
          onKeyDown={handleKeyboard}
          aria-rowcount={rows}
          aria-colcount={8}
          aria-label={`Measurement heatmap; colour scale units ${snapshot ? activeUnits(snapshot).join(", ") : "unknown"}`}
        />
        <div
          className="invalidCellHitLayer"
          style={{ gridTemplateRows: `repeat(${rows}, minmax(0, 1fr))` }}
          aria-label="Invalid and stale measurement cells"
        >
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
              { value: [invalidHover.col, invalidHover.row, 0, cellLabel(invalidHover.row, invalidHover.col), 0] } as never,
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
    grid: { left: 64, right: 28, top: 28, bottom: 72 },
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
      data: [],
      inverse: true,
      splitArea: { show: true },
      axisTick: { show: false }
    }
  };
}

export function activeRows(snapshot: BackendSnapshotPayload | null | undefined): number {
  const value = snapshot?.frame?.rows;
  return Math.max(1, Math.min(8, Math.trunc(typeof value === "number" && Number.isFinite(value) ? value : 8)));
}

export function buildHeatmapData(snapshot: BackendSnapshotPayload): HeatmapDatum[] {
  const data: HeatmapDatum[] = [];
  for (let row = 0; row < activeRows(snapshot); row += 1) {
    const mode = modeForRow(snapshot, row);
    for (let col = 0; col < 8; col += 1) {
      const label = cellLabel(row, col);
      const cell = cellMeasurementState(snapshot.matrix, row, col);
      const displayable = isCellDisplayable(cell);
      data.push([col, row, displayable ? cell.value : null, label, displayable, mode]);
    }
  }
  return data;
}

export function invalidCellTargets(snapshot: BackendSnapshotPayload): Array<CellPoint & { label: string }> {
  if (!snapshot.frame.valid) {
    return [];
  }
  const targets: Array<CellPoint & { label: string }> = [];
  for (let row = 0; row < activeRows(snapshot); row += 1) {
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
  const modes = activeModes(snapshot);
  const cellLabelOption = {
    show: snapshot.display.showCellText,
    formatter: (params: { value?: unknown }) => {
      const value = params.value as [number, number, number | null] | undefined;
      return value ? formatHeatmapCellLabel(snapshot, value[1], value[0], value[2]) : "";
    }
  };
  const series: echarts.SeriesOption[] = modes.map((mode) => ({
    id: `heatmap-${mode.toLowerCase()}`,
    name: `${mode} ${unitForModeInSnapshot(snapshot, mode)}`,
    type: "heatmap",
    data: data
      .filter((item) => item[4] && item[5] === mode)
      .map((item) => ({ value: [item[0], item[1], item[2], item[3], 1] })),
    cursor: "pointer",
    encode: { x: 0, y: 1, value: 2 },
    itemStyle: { borderColor: "#ffffff", borderWidth: 1 },
    emphasis: { itemStyle: { borderColor: "#111827", borderWidth: 2 } },
    label: cellLabelOption,
    tooltip: {
      show: true,
      formatter: (params: echarts.TooltipComponentFormatterCallbackParams) => formatHeatmapTooltip(params, snapshot)
    }
  }));
  const invalidSeriesIndex = series.length;
  series.push({
    id: "invalid-cells",
    type: "heatmap",
    data: data
      .filter((item) => snapshot.frame.valid && !item[4])
      .map((item) => ({ value: [item[0], item[1], 0, item[3], 0] })),
    cursor: "pointer",
    encode: { x: 0, y: 1, value: 2 },
    itemStyle: { color: "#e5e7eb", borderColor: "#ffffff", borderWidth: 1 },
    label: cellLabelOption,
    tooltip: {
      show: true,
      formatter: (params: echarts.TooltipComponentFormatterCallbackParams) => formatHeatmapTooltip(params, snapshot)
    }
  });
  series.push({
    id: "selected-cells",
    type: "scatter",
    symbolSize: 42,
    data: buildSelectedCellData(data, selected),
    itemStyle: { color: "transparent", borderColor: "#111827", borderWidth: 3 },
    tooltip: { show: false },
    silent: true
  });

  const visualMaps = modes.map((mode, index) => {
    const domainData = data.filter((item) => item[4] && item[5] === mode);
    const domain = colourDomainForMode(mode, snapshot.display.displayMode);
    const [min, max] = resolveColourRange(domainData, snapshot, domain);
    return buildDomainVisualMap(min, max, unitForModeInSnapshot(snapshot, mode), domain, index, modes.length, index);
  });
  visualMaps.push({
    id: "invalid-scale",
    show: false,
    min: 0,
    max: 1,
    dimension: 2,
    seriesIndex: [invalidSeriesIndex],
    inRange: { color: ["#e5e7eb"] }
  });

  return {
    xAxis: { data: snapshot.matrix.cols.slice(0, 8) },
    yAxis: { data: rowAxisLabels(snapshot) },
    visualMap: visualMaps,
    series
  };
}

export function buildVisualMapOptions(min: number, max: number, unit: string): NonNullable<echarts.EChartsOption["visualMap"]> {
  return [
    buildDomainVisualMap(min, max, unit, "cap_absolute", 0, 1, 0),
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

function buildDomainVisualMap(
  min: number,
  max: number,
  unit: string,
  domain: ColourDomain,
  seriesIndex: number,
  domainCount: number,
  positionIndex: number
): echarts.VisualMapComponentOption {
  const palettes: Record<ColourDomain, string[]> = {
    cap_absolute: ["#2166ac", "#f7f7f7", "#b2182b"],
    cap_delta: ["#2166ac", "#f7f7f7", "#b2182b"],
    voltage: ["#313695", "#ffffbf", "#a50026"],
    resistance: ["#2c7bb6", "#ffffbf", "#d7191c"]
  };
  const left = domainCount === 1 ? "center" : `${4 + (positionIndex * 92) / domainCount}%`;
  return {
    id: `${domain}-scale`,
    min,
    max,
    dimension: 2,
    seriesIndex: [seriesIndex],
    calculable: false,
    orient: "horizontal",
    left,
    bottom: 8,
    itemWidth: 10,
    itemHeight: domainCount === 1 ? 120 : 82,
    inRange: { color: palettes[domain] },
    text: [unit, ""]
  };
}

export function formatHeatmapCellLabel(
  snapshot: BackendSnapshotPayload,
  row: number,
  col: number,
  plottedValue: number | null
): string {
  if (!snapshot.frame.valid || row < 0 || row >= activeRows(snapshot)) {
    return "";
  }
  const cell = cellMeasurementState(snapshot.matrix, row, col);
  if (!cell.valid) {
    return cell.errorCode === null ? "invalid" : `X${cell.errorCode.toString(16).toUpperCase().padStart(2, "0")}`;
  }
  if (!cell.fresh) {
    return "stale";
  }
  const mode = modeForRow(snapshot, row);
  return formatMeasurementValue(plottedValue, quantityForMode(mode), {
    compact: true,
    percent: unitForRow(snapshot, row) === "%"
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
  const mode = modeForRow(snapshot, row);
  const quantity = quantityForRow(snapshot, row);
  const displayUnit = unitForRow(snapshot, row);
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
    `Error: ${formatErrorCode(cell.errorCode)}${cell.errorReason ? ` \u2014 ${escapeHtml(cell.errorReason)}` : ""}`,
    "<span class=\"tooltipDiagnostics\">Frame diagnostics</span>",
    `Seq: ${snapshot.frame.seq ?? "-"}`,
    `Generation: ${snapshot.frame.profileGeneration ?? snapshot.matrix.generation ?? snapshot.measurement?.generation ?? "-"}`,
    `Request ID: ${snapshot.frame.profileRequestId ?? snapshot.matrix.requestId ?? snapshot.measurement?.requestId ?? "-"}`,
    `Source: ${escapeHtml(snapshot.matrix.sourceTransport || snapshot.connection.mode)}`
  );
  lines.push(...diagnosticTooltipLines(snapshot));
  return lines.join("<br/>");
}

export function pointToCell(chart: echarts.ECharts, offsetX: number, offsetY: number, rows = 8): CellPoint | null {
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
  if (row < 0 || row >= Math.max(1, Math.min(8, rows)) || col < 0 || col >= 8) {
    return null;
  }
  return { row, col };
}

function installHeatmapPointerHandlers(
  chart: echarts.ECharts,
  selectCellRef: MutableRefObject<(cell: string) => void>,
  snapshotRef: MutableRefObject<BackendSnapshotPayload | null>
): () => void {
  const handlePointerDown = (event: { offsetX: number; offsetY: number }) => {
    const point = pointToCell(chart, event.offsetX, event.offsetY, activeRows(snapshotRef.current));
    if (point) {
      selectCellRef.current(cellLabel(point.row, point.col));
    }
  };
  const handleMouseMove = (event: { offsetX: number; offsetY: number }) => {
    const point = pointToCell(chart, event.offsetX, event.offsetY, activeRows(snapshotRef.current));
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
  for (let seriesIndex = 0; seriesIndex < series.length; seriesIndex += 1) {
    const candidate = series[seriesIndex] as { id?: string; type?: string; data?: unknown[] };
    if (candidate.id === "selected-cells" || candidate.type === "scatter") {
      continue;
    }
    const data = candidate.data ?? [];
    const dataIndex = data.findIndex((entry) => {
      const entryValue = entry && typeof entry === "object" && "value" in entry ? (entry as { value: unknown }).value : entry;
      return Array.isArray(entryValue) && Number(entryValue[0]) === point.col && Number(entryValue[1]) === point.row;
    });
    if (dataIndex >= 0) {
      chart.dispatchAction({ type: "showTip", seriesIndex, dataIndex });
      return true;
    }
  }
  chart.dispatchAction({ type: "hideTip" });
  return false;
}

export function heatmapTitle(snapshot: BackendSnapshotPayload): string {
  const modes = activeModes(snapshot);
  const label = modes.length > 1 || snapshot.frame.layout === "MIXED"
    ? "Mixed Measurement"
    : quantityLabel(quantityForMode(modes[0] ?? modeForRow(snapshot, 0)));
  return `${activeRows(snapshot)}x8 ${label} Heatmap`;
}

function activeModes(snapshot: BackendSnapshotPayload): MeasurementMode[] {
  const present = new Set<MeasurementMode>();
  for (let row = 0; row < activeRows(snapshot); row += 1) {
    present.add(modeForRow(snapshot, row));
  }
  return modeOrder.filter((mode) => present.has(mode));
}

function rowAxisLabels(snapshot: BackendSnapshotPayload): string[] {
  const mixed = activeModes(snapshot).length > 1 || snapshot.frame.layout === "MIXED";
  return Array.from({ length: activeRows(snapshot) }, (_, row) => {
    const label = snapshot.matrix.rows?.[row] ?? `S${row + 1}`;
    return mixed ? `${label} \u00B7 ${modeForRow(snapshot, row)}` : label;
  });
}

function activeUnits(snapshot: BackendSnapshotPayload): string[] {
  return activeModes(snapshot).map((mode) => unitForModeInSnapshot(snapshot, mode));
}

function unitForModeInSnapshot(snapshot: BackendSnapshotPayload, mode: MeasurementMode): string {
  for (let row = 0; row < activeRows(snapshot); row += 1) {
    if (modeForRow(snapshot, row) === mode) {
      return unitForRow(snapshot, row);
    }
  }
  return mode === "CAP" ? "pF" : mode === "VOLT" ? "V" : "\u03A9";
}

function firstSelectedPoint(cells: string[], rows: number): CellPoint | null {
  for (const cell of cells) {
    const match = /^S([1-8])D([1-8])$/.exec(cell);
    if (!match) continue;
    const row = Number(match[1]) - 1;
    const col = Number(match[2]) - 1;
    if (row < rows) return { row, col };
  }
  return null;
}

function formatValue(value: number | null | undefined, digits = 3): string {
  if (typeof value !== "number" || Number.isNaN(value)) {
    return "NA";
  }
  return value.toFixed(digits);
}

function diagnosticTooltipLines(snapshot: BackendSnapshotPayload): string[] {
  const diagnostics = snapshot.matrix.diagnostics ?? {};
  const telemetry = snapshot.measurement?.railTelemetry;
  const lines: string[] = [];
  if (typeof telemetry?.railSpanUv === "number") {
    lines.push(`ADS rail span: ${formatMeasurementValue(telemetry.railSpanUv * 1e-6, "voltage")}`);
  }
  if (typeof diagnostics.matrixReferenceUv === "number") {
    lines.push(`Matrix reference: ${formatMeasurementValue(diagnostics.matrixReferenceUv * 1e-6, "voltage")}`);
  }
  if (diagnostics.reference !== undefined && diagnostics.reference !== null) {
    lines.push(`Reference: ${escapeHtml(String(diagnostics.reference))}`);
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
