(function () {
  "use strict";

  const HISTORY_GRAPH_ID = "history-graph";
  const HEATMAP_GRAPH_ID = "heatmap";
  const WARN_INTERVAL_MS = 1500;

  const root = {
    pendingClearRevision: null,
    pendingHeatmapSnapshot: null,
    pendingHistorySnapshot: null,
    rafScheduled: false,
    flushInProgress: false,
    heatmapInitialized: false,
    historyInitialized: false,
    currentHistoryKey: null,
    appliedClearRevision: 0,
    lastClearRevision: 0,
    appliedHeatmapRevision: null,
    appliedHeatmapClearRevision: 0,
    appliedHistoryRevision: null,
    appliedHistoryFollowRevision: null,
    appliedHistoryClearRevision: 0,
    heatmapSelectedCell: null,
    historyX: [],
    historyY: [],
    callbackSamples: [],
    visualSamples: [],
    heatmapSamples: [],
    historySamples: [],
    rafSamples: [],
    browserRafFps: 0,
    coalescedFrames: 0,
    coalescedHeatmapUpdates: 0,
    coalescedHistoryUpdates: 0,
    droppedFrames: 0,
    historyRetryCount: 0,
    heatmapRetryCount: 0,
    clearRetryCount: 0,
    lastClientError: "",
    lastHistoryError: "",
    lastHeatmapError: "",
    lastWarnByKey: {},
    programmaticHistoryRelayout: false,
    programmaticHistoryRelayoutUntil: 0
  };

  function graphDiv(id) {
    const outer = document.getElementById(id);
    if (!outer) {
      return null;
    }
    if (outer.classList && outer.classList.contains("js-plotly-plot")) {
      return outer;
    }
    return outer.querySelector(".js-plotly-plot");
  }

  function isPlotlyDivReady(div) {
    return Boolean(window.Plotly && div && div.classList && div.classList.contains("js-plotly-plot"));
  }

  function labels(prefix) {
    return Array.from({ length: 8 }, function (_, i) { return prefix + String(i + 1); });
  }

  function selectedXY(cell) {
    const match = /^S([1-8])D([1-8])$/.exec(cell || "");
    if (!match) {
      return { x: [], y: [] };
    }
    return { x: ["D" + match[2]], y: ["S" + match[1]] };
  }

  function nowSeconds() {
    return performance.now() / 1000.0;
  }

  function prune(samples, now) {
    while (samples.length && samples[0] < now - 2.0) {
      samples.shift();
    }
  }

  function fps(samples) {
    if (samples.length < 2) {
      return 0.0;
    }
    return (samples.length - 1) / Math.max(0.001, samples[samples.length - 1] - samples[0]);
  }

  function record(samples) {
    const now = nowSeconds();
    samples.push(now);
    prune(samples, now);
  }

  function recordVisual(kind) {
    record(root.visualSamples);
    if (kind === "history") {
      record(root.historySamples);
    } else if (kind === "heatmap") {
      record(root.heatmapSamples);
    }
  }

  function numberOrNull(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function clearRevisionOf(value) {
    if (!value) {
      return 0;
    }
    const state = typeof value === "object" ? value : { revision: value };
    const parsed = Number(state.clearRevision ?? state.revision ?? 0);
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function asArray(value) {
    if (!value) {
      return [];
    }
    if (Array.isArray(value)) {
      return value.slice();
    }
    if (typeof value.length === "number") {
      return Array.prototype.slice.call(value);
    }
    return [];
  }

  function finiteNumbers(values) {
    return asArray(values).map(numberOrNull).filter(function (value) { return value !== null; });
  }

  function windowSeconds(windowName) {
    return {
      last_10s: 10,
      last_30s: 30,
      last_60s: 60,
      last_5min: 300
    }[windowName || ""];
  }

  function historyWindowSpan(snapshot) {
    const seconds = windowSeconds(snapshot.historyWindow);
    if (!seconds) {
      return null;
    }
    const axis = snapshot.xAxis || "timeSeconds";
    if (axis === "timestampUs") {
      return seconds * 1000000.0;
    }
    if (axis === "timeSeconds") {
      return seconds;
    }
    return null;
  }

  function axisMinimumPadding(snapshot) {
    return (snapshot.xAxis || "timeSeconds") === "timestampUs" ? 500000.0 : 0.5;
  }

  function ensurePaddedRange(start, end, snapshot) {
    let xStart = numberOrNull(start);
    let xEnd = numberOrNull(end);
    if (xStart === null || xEnd === null) {
      return null;
    }
    if (xEnd < xStart) {
      const tmp = xStart;
      xStart = xEnd;
      xEnd = tmp;
    }
    if (xEnd > xStart) {
      return [xStart, xEnd];
    }
    const span = historyWindowSpan(snapshot);
    const pad = Math.max(axisMinimumPadding(snapshot), Number.isFinite(span) && span > 0 ? Math.abs(span) * 0.05 : 0);
    return [xStart - pad, xEnd + pad];
  }

  function snapshotFollowRange(snapshot) {
    const start = numberOrNull(snapshot.followRangeStart);
    const end = numberOrNull(snapshot.followRangeEnd);
    if (start !== null && end !== null) {
      return ensurePaddedRange(start, end, snapshot);
    }
    return null;
  }

  function computeFollowXRange(snapshot, xValues) {
    const explicitRange = snapshotFollowRange(snapshot);
    if (explicitRange) {
      return explicitRange;
    }
    const values = finiteNumbers(xValues);
    if (!values.length) {
      return null;
    }
    const latest = values[values.length - 1];
    const span = historyWindowSpan(snapshot);
    if (span && Number.isFinite(latest)) {
      return ensurePaddedRange(latest - span, latest, snapshot);
    }
    if (values.length === 1) {
      return ensurePaddedRange(values[0], values[0], snapshot);
    }
    return ensurePaddedRange(Math.min.apply(null, values), Math.max.apply(null, values), snapshot);
  }

  function computeVisibleYRange(xValues, yValues, xStart, xEnd) {
    const x = asArray(xValues);
    const y = asArray(yValues);
    const visible = [];
    for (let i = 0; i < y.length; i += 1) {
      const xValue = numberOrNull(x[i]);
      const yValue = numberOrNull(y[i]);
      if (xValue !== null && yValue !== null && xValue >= xStart && xValue <= xEnd) {
        visible.push(yValue);
      }
    }
    const source = visible.length ? visible : finiteNumbers(y);
    if (!source.length) {
      return null;
    }
    const yMin = Math.min.apply(null, source);
    const yMax = Math.max.apply(null, source);
    let margin;
    if (yMin === yMax) {
      margin = Math.max(Math.abs(yMin) * 0.01, 1e-6);
    } else {
      margin = Math.max((yMax - yMin) * 0.05, Math.abs(yMax) * 0.005, 1e-9);
    }
    return [yMin - margin, yMax + margin];
  }

  async function relayoutHistory(div, update) {
    root.programmaticHistoryRelayout = true;
    root.programmaticHistoryRelayoutUntil = performance.now() + 80;
    try {
      await Plotly.relayout(div, update);
    } finally {
      window.setTimeout(function () {
        if (performance.now() >= root.programmaticHistoryRelayoutUntil) {
          root.programmaticHistoryRelayout = false;
        }
      }, 90);
    }
  }

  async function applyFollowRange(div, snapshot, xValues, yValues) {
    if (!snapshot.followLatest) {
      return { didRelayout: false };
    }
    const xRange = computeFollowXRange(snapshot, xValues);
    if (!xRange) {
      return { didRelayout: false };
    }
    const yRange = computeVisibleYRange(xValues, yValues, xRange[0], xRange[1]);
    const update = {
      "xaxis.autorange": false,
      "xaxis.range": xRange
    };
    if (yRange) {
      update["yaxis.autorange"] = false;
      update["yaxis.range"] = yRange;
    } else {
      update["yaxis.autorange"] = true;
    }
    await relayoutHistory(div, update);
    return { didRelayout: true, xRange: xRange, yRange: yRange };
  }

  function historyTraceExists(div) {
    return Boolean(div && div.data && div.data[0] && Array.isArray(div.data[0].x) && Array.isArray(div.data[0].y));
  }

  function currentTraceValues(div) {
    const trace = div && div.data && div.data[0];
    return {
      x: trace ? asArray(trace.x) : [],
      y: trace ? asArray(trace.y) : []
    };
  }

  function trimToMax(values, maxPoints) {
    const max = Math.max(1, Number(maxPoints) || 1200);
    return values.length > max ? values.slice(values.length - max) : values;
  }

  function buildHistoryLayout(snapshot) {
    const title = snapshot.title || ("History of " + (snapshot.selectedCell || "-") + " / " + (snapshot.stream || "-"));
    const resetToken = snapshot.resetNonce ?? snapshot.clearRevision ?? snapshot.cacheRevision ?? 0;
    const uirevision = snapshot.followLatest
      ? String(snapshot.key || "history") + "|follow|" + String(snapshot.followRevision || 0) + "|reset|" + String(resetToken)
      : String(snapshot.key || "history") + "|manual";
    return {
      title: title,
      margin: { l: 58, r: 24, t: 48, b: 50 },
      paper_bgcolor: "white",
      plot_bgcolor: "white",
      font: { family: "Segoe UI, Arial, sans-serif", size: 12, color: "#17202a" },
      xaxis: { title: snapshot.xAxis || "timeSeconds", showgrid: true, gridcolor: "#e5e7eb", autorange: true },
      yaxis: { title: "value (" + (snapshot.unit || "uV") + ")", showgrid: true, gridcolor: "#e5e7eb", autorange: true },
      uirevision: uirevision
    };
  }

  function buildHistoryTrace(snapshot, xValues, yValues) {
    return {
      type: "scattergl",
      mode: snapshot.showMarkers ? "lines+markers" : "lines",
      x: xValues,
      y: yValues,
      name: (snapshot.selectedCell || "-") + " / " + (snapshot.stream || "-"),
      line: { color: "#0f766e", width: 2 },
      marker: { size: 4 },
      hovertemplate: "%{x}<br>%{y}<extra></extra>"
    };
  }

  function appendSnapshotValues(snapshot) {
    const x = asArray(snapshot.xAppend || snapshot.x);
    const y = asArray(snapshot.yAppend || snapshot.y);
    return { x: x, y: y };
  }

  function fullResetValues(snapshot, div) {
    const maxPoints = snapshot.maxPoints || 1200;
    const snapshotX = asArray(snapshot.x);
    const snapshotY = asArray(snapshot.y);
    if (!snapshot.reset && root.currentHistoryKey === snapshot.key && root.historyX.length) {
      const appendValues = appendSnapshotValues(snapshot);
      return {
        x: trimToMax(root.historyX.concat(appendValues.x), maxPoints),
        y: trimToMax(root.historyY.concat(appendValues.y), maxPoints)
      };
    }
    if (!snapshot.reset && historyTraceExists(div) && !snapshotX.length) {
      const current = currentTraceValues(div);
      const appendValues = appendSnapshotValues(snapshot);
      return {
        x: trimToMax(current.x.concat(appendValues.x), maxPoints),
        y: trimToMax(current.y.concat(appendValues.y), maxPoints)
      };
    }
    return {
      x: trimToMax(snapshotX, maxPoints),
      y: trimToMax(snapshotY, maxPoints)
    };
  }

  async function applyHistoryReset(div, snapshot, fallback) {
    const values = fullResetValues(snapshot, div);
    const layout = buildHistoryLayout(snapshot);
    await Plotly.react(
      div,
      [buildHistoryTrace(snapshot, values.x, values.y)],
      layout,
      { displayModeBar: true, responsive: true, scrollZoom: true }
    );
    const follow = await applyFollowRange(div, snapshot, values.x, values.y);
    recordVisual("history");
    return {
      ok: true,
      appliedRevision: snapshot.cacheRevision,
      appliedFollowRevision: snapshot.followRevision,
      appliedClearRevision: clearRevisionOf(snapshot),
      historyKey: snapshot.key,
      historyX: values.x,
      historyY: values.y,
      didReset: true,
      didAppend: false,
      didRelayout: follow.didRelayout,
      fallback: Boolean(fallback)
    };
  }

  async function applyHistoryAppend(div, snapshot) {
    const appendValues = appendSnapshotValues(snapshot);
    const maxPoints = snapshot.maxPoints || 1200;
    if (!appendValues.x.length && !appendValues.y.length) {
      let didRelayout = false;
      if (snapshot.followLatest && snapshot.followRevision !== root.appliedHistoryFollowRevision) {
        const current = currentTraceValues(div);
        const follow = await applyFollowRange(div, snapshot, current.x, current.y);
        didRelayout = follow.didRelayout;
        if (didRelayout) {
          recordVisual("history");
        }
      }
      return {
        ok: true,
        appliedRevision: snapshot.cacheRevision,
        appliedFollowRevision: snapshot.followRevision,
        appliedClearRevision: clearRevisionOf(snapshot),
        historyKey: snapshot.key,
        historyX: root.historyX,
        historyY: root.historyY,
        didReset: false,
        didAppend: false,
        didRelayout: didRelayout
      };
    }

    try {
      await Plotly.extendTraces(div, { x: [appendValues.x], y: [appendValues.y] }, [0], maxPoints);
    } catch (error) {
      warnPlotly("history", "extend-failed-reset-fallback", snapshot, error);
      return applyHistoryReset(div, snapshot, true);
    }

    const current = currentTraceValues(div);
    const nextX = trimToMax(current.x, maxPoints);
    const nextY = trimToMax(current.y, maxPoints);
    const follow = await applyFollowRange(div, snapshot, nextX, nextY);
    recordVisual("history");
    return {
      ok: true,
      appliedRevision: snapshot.cacheRevision,
      appliedFollowRevision: snapshot.followRevision,
      appliedClearRevision: clearRevisionOf(snapshot),
      historyKey: snapshot.key,
      historyX: nextX,
      historyY: nextY,
      didReset: false,
      didAppend: true,
      didRelayout: follow.didRelayout
    };
  }

  async function applyHistoryNow(snapshot) {
    if (!snapshot || snapshot.kind !== "history") {
      return { ok: true, ignored: true };
    }
    if (!window.Plotly) {
      return failResult("history", "plotly-not-loaded", snapshot);
    }
    const div = graphDiv(HISTORY_GRAPH_ID);
    if (!isPlotlyDivReady(div)) {
      return failResult("history", "plotly-div-not-ready", snapshot);
    }
    if (clearRevisionOf(snapshot) < root.lastClearRevision) {
      return { ok: true, ignored: true, stale: true };
    }

    const current = currentTraceValues(div);
    const snapshotHasPoints = asArray(snapshot.x).length > 0 || asArray(snapshot.xAppend).length > 0;
    const needReset = Boolean(snapshot.reset) ||
      !root.historyInitialized ||
      !root.currentHistoryKey ||
      root.currentHistoryKey !== snapshot.key ||
      !historyTraceExists(div) ||
      (current.x.length === 0 && snapshotHasPoints) ||
      clearRevisionOf(snapshot) !== root.appliedHistoryClearRevision;

    try {
      if (needReset) {
        return await applyHistoryReset(div, snapshot, false);
      }
      return await applyHistoryAppend(div, snapshot);
    } catch (error) {
      return failResult("history", "plotly-update-failed", snapshot, error);
    }
  }

  function buildHeatmapLayout() {
    return {
      title: "8x8 Matrix",
      margin: { l: 50, r: 58, t: 48, b: 38 },
      paper_bgcolor: "white",
      plot_bgcolor: "white",
      font: { family: "Segoe UI, Arial, sans-serif", size: 12, color: "#17202a" },
      clickmode: "event+select",
      xaxis: { side: "top", constrain: "domain" },
      yaxis: { autorange: "reversed", scaleanchor: "x", scaleratio: 1 }
    };
  }

  function buildHeatmapData(snapshot) {
    const z = snapshot.matrix || snapshot.matrixUv || [];
    const unit = snapshot.unit || "uV";
    const text = snapshot.cellText || [];
    const cellNames = snapshot.cellNames || [];
    const xy = selectedXY(snapshot.selectedCell);
    const colorbar = { title: { text: unit }, len: 0.82, thickness: 12, y: 0.47 };
    const fixedRange = Number.isFinite(Number(snapshot.zmin)) && Number.isFinite(Number(snapshot.zmax));
    const heatmapTrace = {
      type: "heatmap",
      z: z,
      x: labels("D"),
      y: labels("S"),
      text: text,
      customdata: cellNames,
      texttemplate: "%{text}",
      textfont: { size: 10, color: "#111827" },
      colorscale: "RdYlBu",
      reversescale: true,
      colorbar: colorbar,
      hovertemplate: "cell=%{customdata}<br>value=%{z:,.3g} " + unit + "<br>seq=" + (snapshot.seq ?? "-") + "<extra></extra>",
      zauto: !fixedRange,
      xgap: 1,
      ygap: 1
    };
    if (fixedRange) {
      heatmapTrace.zmin = Number(snapshot.zmin);
      heatmapTrace.zmax = Number(snapshot.zmax);
    }
    return {
      trace: heatmapTrace,
      selectedTrace: {
        type: "scatter",
        mode: "markers",
        x: xy.x,
        y: xy.y,
        marker: { symbol: "square-open", size: 58, line: { color: "#111827", width: 3 } },
        hoverinfo: "skip",
        showlegend: false
      },
      colorbar: colorbar,
      fixedRange: fixedRange,
      xy: xy
    };
  }

  async function applyHeatmapNow(snapshot) {
    if (!snapshot || snapshot.kind !== "heatmap") {
      return { ok: true, ignored: true };
    }
    if (!window.Plotly) {
      return failResult("heatmap", "plotly-not-loaded", snapshot);
    }
    const div = graphDiv(HEATMAP_GRAPH_ID);
    if (!isPlotlyDivReady(div)) {
      return failResult("heatmap", "plotly-div-not-ready", snapshot);
    }
    if (clearRevisionOf(snapshot) < root.lastClearRevision) {
      return { ok: true, ignored: true, stale: true };
    }

    const built = buildHeatmapData(snapshot);
    try {
      if (!root.heatmapInitialized || clearRevisionOf(snapshot) !== root.appliedHeatmapClearRevision) {
        await Plotly.react(div, [built.trace, built.selectedTrace], buildHeatmapLayout(), { displayModeBar: false, responsive: true });
      } else {
        const update = {
          z: [built.trace.z],
          text: [built.trace.text],
          customdata: [built.trace.customdata],
          colorbar: [built.colorbar],
          hovertemplate: [built.trace.hovertemplate],
          zauto: [!built.fixedRange]
        };
        if (built.fixedRange) {
          update.zmin = [Number(snapshot.zmin)];
          update.zmax = [Number(snapshot.zmax)];
        }
        await Plotly.restyle(div, update, [0]);
        if (root.heatmapSelectedCell !== snapshot.selectedCell) {
          await Plotly.restyle(div, { x: [built.xy.x], y: [built.xy.y] }, [1]);
        }
      }
      recordVisual("heatmap");
      return {
        ok: true,
        appliedRevision: snapshot.cacheRevision,
        appliedClearRevision: clearRevisionOf(snapshot),
        selectedCell: snapshot.selectedCell
      };
    } catch (error) {
      return failResult("heatmap", "plotly-update-failed", snapshot, error);
    }
  }

  function emptyHistoryLayout() {
    return {
      title: "History",
      margin: { l: 58, r: 24, t: 48, b: 50 },
      paper_bgcolor: "white",
      plot_bgcolor: "white",
      font: { family: "Segoe UI, Arial, sans-serif", size: 12, color: "#17202a" },
      xaxis: { title: "timeSeconds", showgrid: true, gridcolor: "#e5e7eb", autorange: true },
      yaxis: { title: "value", showgrid: true, gridcolor: "#e5e7eb", autorange: true },
      uirevision: "history|cleared|" + String(root.lastClearRevision)
    };
  }

  async function applyClearNow(clearRevision) {
    if (!window.Plotly) {
      return failResult("clear", "plotly-not-loaded", { clearRevision: clearRevision });
    }
    const historyDiv = graphDiv(HISTORY_GRAPH_ID);
    const heatmapDiv = graphDiv(HEATMAP_GRAPH_ID);
    if (!isPlotlyDivReady(historyDiv) && !isPlotlyDivReady(heatmapDiv)) {
      return failResult("clear", "plotly-div-not-ready", { clearRevision: clearRevision });
    }

    root.historyInitialized = false;
    root.currentHistoryKey = null;
    root.historyX = [];
    root.historyY = [];
    root.heatmapInitialized = false;
    root.heatmapSelectedCell = null;
    root.appliedHistoryRevision = null;
    root.appliedHistoryFollowRevision = null;
    root.appliedHeatmapRevision = null;
    root.appliedHistoryClearRevision = clearRevision;
    root.appliedHeatmapClearRevision = clearRevision;

    try {
      if (isPlotlyDivReady(historyDiv)) {
        await Plotly.react(historyDiv, [], emptyHistoryLayout(), { displayModeBar: true, responsive: true, scrollZoom: true });
      }
      if (isPlotlyDivReady(heatmapDiv)) {
        await Plotly.react(heatmapDiv, [], buildHeatmapLayout(), { displayModeBar: false, responsive: true });
      }
      recordVisual("history");
      return { ok: true, appliedClearRevision: clearRevision };
    } catch (error) {
      return failResult("clear", "plotly-clear-failed", { clearRevision: clearRevision }, error);
    }
  }

  function failResult(kind, reason, snapshot, error) {
    const message = reason + (error ? ": " + String(error && error.message ? error.message : error) : "");
    root.lastClientError = message;
    if (kind === "history") {
      root.lastHistoryError = message;
    } else if (kind === "heatmap") {
      root.lastHeatmapError = message;
    }
    return { ok: false, reason: reason, error: error };
  }

  function warnPlotly(kind, reason, snapshot, error) {
    const key = kind + ":" + reason;
    const now = performance.now();
    const last = root.lastWarnByKey[key] || 0;
    if (now - last < WARN_INTERVAL_MS) {
      return;
    }
    root.lastWarnByKey[key] = now;
    console.warn("SensorArray live Plotly update retry", {
      kind: kind,
      reason: reason,
      key: snapshot && snapshot.key,
      cacheRevision: snapshot && snapshot.cacheRevision,
      followRevision: snapshot && snapshot.followRevision,
      clearRevision: snapshot && (snapshot.clearRevision ?? snapshot.revision),
      error: error ? String(error && error.message ? error.message : error) : undefined
    });
  }

  function commitHistoryResult(result) {
    if (!result || result.ignored) {
      return;
    }
    root.appliedHistoryRevision = result.appliedRevision;
    root.appliedHistoryFollowRevision = result.appliedFollowRevision;
    root.appliedHistoryClearRevision = result.appliedClearRevision;
    root.currentHistoryKey = result.historyKey;
    root.historyInitialized = true;
    root.historyX = asArray(result.historyX);
    root.historyY = asArray(result.historyY);
    root.historyRetryCount = 0;
    root.lastHistoryError = "";
    root.lastClientError = "";
  }

  function commitHeatmapResult(result) {
    if (!result || result.ignored) {
      return;
    }
    root.appliedHeatmapRevision = result.appliedRevision;
    root.appliedHeatmapClearRevision = result.appliedClearRevision;
    root.heatmapInitialized = true;
    root.heatmapSelectedCell = result.selectedCell;
    root.heatmapRetryCount = 0;
    root.lastHeatmapError = "";
    root.lastClientError = "";
  }

  async function flush() {
    if (root.flushInProgress) {
      root.rafScheduled = false;
      scheduleFlush();
      return;
    }
    root.rafScheduled = false;
    root.flushInProgress = true;
    let shouldRetry = false;
    try {
      if (root.pendingClearRevision !== null) {
        const clearRevision = root.pendingClearRevision;
        const result = await applyClearNow(clearRevision);
        if (result.ok) {
          root.appliedClearRevision = clearRevision;
          root.pendingClearRevision = null;
          root.clearRetryCount = 0;
        } else {
          root.clearRetryCount += 1;
          shouldRetry = true;
          warnPlotly("clear", result.reason || "failed", { clearRevision: clearRevision }, result.error);
        }
      }

      if (root.pendingHistorySnapshot) {
        const snapshot = root.pendingHistorySnapshot;
        const result = await applyHistoryNow(snapshot);
        if (result.ok) {
          commitHistoryResult(result);
          root.pendingHistorySnapshot = null;
        } else {
          root.historyRetryCount += 1;
          shouldRetry = true;
          warnPlotly("history", result.reason || "failed", snapshot, result.error);
        }
      }

      if (root.pendingHeatmapSnapshot) {
        const snapshot = root.pendingHeatmapSnapshot;
        const result = await applyHeatmapNow(snapshot);
        if (result.ok) {
          commitHeatmapResult(result);
          root.pendingHeatmapSnapshot = null;
        } else {
          root.heatmapRetryCount += 1;
          shouldRetry = true;
          warnPlotly("heatmap", result.reason || "failed", snapshot, result.error);
        }
      }
    } catch (error) {
      shouldRetry = true;
      root.lastClientError = String(error && error.message ? error.message : error);
      console.error("SensorArray live Plotly update failed", error);
    } finally {
      root.flushInProgress = false;
      if (shouldRetry || root.pendingClearRevision !== null || root.pendingHistorySnapshot || root.pendingHeatmapSnapshot) {
        scheduleFlush();
      }
    }
  }

  function scheduleFlush() {
    if (root.rafScheduled) {
      root.coalescedFrames += 1;
      return;
    }
    root.rafScheduled = true;
    window.requestAnimationFrame(function () {
      void flush();
    });
  }

  function handleClearRevision(clearRevisionState) {
    const revision = clearRevisionOf(clearRevisionState);
    if (revision <= root.lastClearRevision) {
      return false;
    }
    root.lastClearRevision = revision;
    root.pendingClearRevision = revision;
    root.pendingHeatmapSnapshot = null;
    root.pendingHistorySnapshot = null;
    root.historyInitialized = false;
    root.currentHistoryKey = null;
    root.historyX = [];
    root.historyY = [];
    root.heatmapInitialized = false;
    root.heatmapSelectedCell = null;
    root.appliedHistoryRevision = null;
    root.appliedHistoryFollowRevision = null;
    root.appliedHeatmapRevision = null;
    return true;
  }

  function shouldApplyHistory(snapshot) {
    if (!snapshot || snapshot.kind !== "history") {
      return false;
    }
    if (clearRevisionOf(snapshot) < root.lastClearRevision) {
      return false;
    }
    if (snapshot.cacheRevision !== root.appliedHistoryRevision) {
      return true;
    }
    if (snapshot.followRevision !== root.appliedHistoryFollowRevision) {
      return true;
    }
    if (clearRevisionOf(snapshot) !== root.appliedHistoryClearRevision) {
      return true;
    }
    return false;
  }

  function shouldApplyHeatmap(snapshot) {
    if (!snapshot || snapshot.kind !== "heatmap") {
      return false;
    }
    if (clearRevisionOf(snapshot) < root.lastClearRevision) {
      return false;
    }
    if (snapshot.cacheRevision !== root.appliedHeatmapRevision) {
      return true;
    }
    if (clearRevisionOf(snapshot) !== root.appliedHeatmapClearRevision) {
      return true;
    }
    return false;
  }

  function buildFrontendStats() {
    return {
      lastHeatmapRevision: root.appliedHeatmapRevision,
      lastHistoryRevision: root.appliedHistoryRevision,
      lastHistoryFollowRevision: root.appliedHistoryFollowRevision,
      lastClearRevision: root.appliedClearRevision,
      browserRafFps: root.browserRafFps,
      visualUpdateFps: fps(root.visualSamples),
      callbackFps: fps(root.callbackSamples),
      heatmapActualFps: fps(root.heatmapSamples),
      historyActualFps: fps(root.historySamples),
      coalescedFrames: root.coalescedFrames,
      coalescedHeatmapUpdates: root.coalescedHeatmapUpdates,
      coalescedHistoryUpdates: root.coalescedHistoryUpdates,
      droppedFrames: root.droppedFrames,
      frontendRenderSkipped: root.droppedFrames,
      pendingHistory: Boolean(root.pendingHistorySnapshot),
      pendingHeatmap: Boolean(root.pendingHeatmapSnapshot),
      historyRetryCount: root.historyRetryCount,
      heatmapRetryCount: root.heatmapRetryCount,
      lastClientError: root.lastClientError,
      lastHistoryError: root.lastHistoryError,
      lastHeatmapError: root.lastHeatmapError,
      programmaticHistoryRelayout: root.programmaticHistoryRelayout
    };
  }

  root.applySnapshots = function (heatmapSnapshot, historySnapshot, clearRevisionState, _statusTick, _current) {
    record(root.callbackSamples);
    const clearChanged = handleClearRevision(clearRevisionState);

    if (shouldApplyHistory(historySnapshot)) {
      if (root.pendingHistorySnapshot && root.pendingHistorySnapshot.cacheRevision !== historySnapshot.cacheRevision) {
        root.coalescedHistoryUpdates += 1;
      }
      root.pendingHistorySnapshot = historySnapshot;
    }
    if (shouldApplyHeatmap(heatmapSnapshot)) {
      if (root.pendingHeatmapSnapshot && root.pendingHeatmapSnapshot.cacheRevision !== heatmapSnapshot.cacheRevision) {
        root.coalescedHeatmapUpdates += 1;
      }
      root.pendingHeatmapSnapshot = heatmapSnapshot;
    }
    if (clearChanged || root.pendingHistorySnapshot || root.pendingHeatmapSnapshot) {
      scheduleFlush();
    }

    return buildFrontendStats();
  };

  function startBrowserRafCounter() {
    window.requestAnimationFrame(function tick() {
      record(root.rafSamples);
      root.browserRafFps = fps(root.rafSamples);
      window.requestAnimationFrame(tick);
    });
  }

  startBrowserRafCounter();
  window.SensorArrayLive = root;
})();
