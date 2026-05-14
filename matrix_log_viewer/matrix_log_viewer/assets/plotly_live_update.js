(function () {
  "use strict";

  const root = {
    pendingHeatmapSnapshot: null,
    pendingHistorySnapshot: null,
    rafScheduled: false,
    heatmapInitialized: false,
    historyInitialized: false,
    currentHistoryKey: null,
    heatmapSelectedCell: null,
    coalescedFrames: 0,
    droppedFrames: 0,
    lastClientError: "",
    heatmapSamples: [],
    historySamples: [],
    historyPromise: Promise.resolve()
  };

  function graphDiv(id) {
    const outer = document.getElementById(id);
    if (!outer) {
      return null;
    }
    return outer.querySelector(".js-plotly-plot") || outer;
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

  function numberOrNull(value) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }

  function windowSeconds(windowName) {
    return {
      last_10s: 10,
      last_30s: 30,
      last_60s: 60,
      last_5min: 300
    }[windowName || ""];
  }

  function visibleXValues(div, fallback) {
    const trace = div && div.data && div.data[0];
    const values = trace && Array.isArray(trace.x) ? trace.x : (fallback || []);
    return values.map(numberOrNull).filter(function (value) { return value !== null; });
  }

  function snapshotFollowRange(snapshot) {
    const start = numberOrNull(snapshot.followRangeStart);
    const end = numberOrNull(snapshot.followRangeEnd);
    if (start !== null && end !== null && end > start) {
      return [start, end];
    }
    return null;
  }

  function applyFollowRange(div, snapshot) {
    if (!snapshot.followLatest) {
      return Promise.resolve();
    }
    const explicitRange = snapshotFollowRange(snapshot);
    if (explicitRange) {
      return Promise.resolve(Plotly.relayout(div, { "xaxis.range": explicitRange }));
    }
    const values = visibleXValues(div, snapshot.x || []);
    if (!values.length) {
      return Promise.resolve();
    }
    const latest = values[values.length - 1];
    let earliest = values[0];
    const seconds = windowSeconds(snapshot.historyWindow);
    if ((snapshot.xAxis || "timeSeconds") === "timeSeconds" && seconds) {
      earliest = latest - seconds;
    }
    if (Number.isFinite(earliest) && Number.isFinite(latest) && latest > earliest) {
      return Promise.resolve(Plotly.relayout(div, { "xaxis.range": [earliest, latest] }));
    }
    return Promise.resolve();
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

  function applyHeatmap(snapshot) {
    if (!snapshot || snapshot.kind !== "heatmap") {
      return;
    }
    const div = graphDiv("heatmap");
    if (!div || !window.Plotly) {
      return;
    }
    const z = snapshot.matrix || snapshot.matrixUv || [];
    const unit = snapshot.unit || "uV";
    const text = snapshot.cellText || [];
    const cellNames = snapshot.cellNames || [];
    const xy = selectedXY(snapshot.selectedCell);
    const layout = {
      title: "8x8 Matrix",
      margin: { l: 50, r: 58, t: 48, b: 38 },
      paper_bgcolor: "white",
      plot_bgcolor: "white",
      font: { family: "Segoe UI, Arial, sans-serif", size: 12, color: "#17202a" },
      clickmode: "event+select",
      xaxis: { side: "top", constrain: "domain" },
      yaxis: { autorange: "reversed", scaleanchor: "x", scaleratio: 1 }
    };
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
    if (!root.heatmapInitialized) {
      Plotly.newPlot(div, [
        heatmapTrace,
        {
          type: "scatter",
          mode: "markers",
          x: xy.x,
          y: xy.y,
          marker: { symbol: "square-open", size: 58, line: { color: "#111827", width: 3 } },
          hoverinfo: "skip",
          showlegend: false
        }
      ], layout, { displayModeBar: false, responsive: true });
      root.heatmapInitialized = true;
      root.heatmapSelectedCell = snapshot.selectedCell;
    } else {
      const update = {
        z: [z],
        text: [text],
        customdata: [cellNames],
        colorbar: [colorbar],
        hovertemplate: [heatmapTrace.hovertemplate],
        zauto: [!fixedRange]
      };
      if (fixedRange) {
        update.zmin = [Number(snapshot.zmin)];
        update.zmax = [Number(snapshot.zmax)];
      }
      Plotly.restyle(div, update, [0]);
      if (root.heatmapSelectedCell !== snapshot.selectedCell) {
        Plotly.restyle(div, { x: [xy.x], y: [xy.y] }, [1]);
        root.heatmapSelectedCell = snapshot.selectedCell;
      }
    }
    record(root.heatmapSamples);
  }

  function applyHistory(snapshot) {
    if (!snapshot || snapshot.kind !== "history") {
      return Promise.resolve();
    }
    root.historyPromise = root.historyPromise.catch(function () {
      return undefined;
    }).then(function () {
      return applyHistoryNow(snapshot);
    });
    return root.historyPromise;
  }

  function applyHistoryNow(snapshot) {
    if (!snapshot || snapshot.kind !== "history") {
      return Promise.resolve();
    }
    const div = graphDiv("history-graph");
    if (!div || !window.Plotly) {
      return Promise.resolve();
    }
    const title = snapshot.title || ("History of " + snapshot.selectedCell + " / " + snapshot.stream);
    const layout = {
      title: title,
      margin: { l: 58, r: 24, t: 48, b: 50 },
      paper_bgcolor: "white",
      plot_bgcolor: "white",
      font: { family: "Segoe UI, Arial, sans-serif", size: 12, color: "#17202a" },
      xaxis: { title: snapshot.xAxis || "timeSeconds", showgrid: true, gridcolor: "#e5e7eb" },
      yaxis: { title: "value (" + (snapshot.unit || "uV") + ")", showgrid: true, gridcolor: "#e5e7eb", autorange: true },
      uirevision: snapshot.key
    };
    const mode = snapshot.showMarkers ? "lines+markers" : "lines";
    if (snapshot.reset || !root.historyInitialized || root.currentHistoryKey !== snapshot.key) {
      const reactPromise = Promise.resolve(Plotly.react(div, [{
        type: "scattergl",
        mode: mode,
        x: snapshot.x || [],
        y: snapshot.y || [],
        name: (snapshot.selectedCell || "-") + " / " + (snapshot.stream || "-"),
        line: { color: "#0f766e", width: 2 },
        hovertemplate: "%{x}<br>%{y}<extra></extra>"
      }], layout, { displayModeBar: true, responsive: true, scrollZoom: true }));
      return reactPromise.then(function () {
        root.historyInitialized = true;
        root.currentHistoryKey = snapshot.key;
        return applyFollowRange(div, snapshot);
      }).then(function () {
        record(root.historySamples);
      });
    } else if (snapshot.key === root.currentHistoryKey) {
      const x = snapshot.x || [];
      const y = snapshot.y || [];
      if (x.length || y.length) {
        return Promise.resolve(Plotly.extendTraces(div, { x: [x], y: [y] }, [0], snapshot.maxPoints || 1200)).then(function () {
          return applyFollowRange(div, snapshot);
        }).then(function () {
          record(root.historySamples);
        });
      }
      return applyFollowRange(div, snapshot).then(function () {
        record(root.historySamples);
      });
    } else {
      root.droppedFrames += 1;
      return Promise.resolve();
    }
  }

  function flush() {
    root.rafScheduled = false;
    const heatmap = root.pendingHeatmapSnapshot;
    const history = root.pendingHistorySnapshot;
    root.pendingHeatmapSnapshot = null;
    root.pendingHistorySnapshot = null;
    try {
      applyHeatmap(heatmap);
      Promise.resolve(applyHistory(history)).then(function () {
        root.lastClientError = "";
      }).catch(function (error) {
        root.lastClientError = String(error && error.message ? error.message : error);
        console.error("SensorArray live Plotly update failed", error);
      });
    } catch (error) {
      root.lastClientError = String(error && error.message ? error.message : error);
      console.error("SensorArray live Plotly update failed", error);
    }
  }

  function schedule() {
    if (root.rafScheduled) {
      root.coalescedFrames += 1;
      return;
    }
    root.rafScheduled = true;
    window.requestAnimationFrame(flush);
  }

  root.applySnapshots = function (heatmapSnapshot, historySnapshot, current) {
    if (heatmapSnapshot && heatmapSnapshot.cacheRevision !== (current || {}).lastHeatmapRevision) {
      root.pendingHeatmapSnapshot = heatmapSnapshot;
    }
    if (
      historySnapshot &&
      (
        historySnapshot.cacheRevision !== (current || {}).lastHistoryRevision ||
        historySnapshot.followRevision !== (current || {}).lastHistoryFollowRevision
      )
    ) {
      root.pendingHistorySnapshot = historySnapshot;
    }
    if (root.pendingHeatmapSnapshot || root.pendingHistorySnapshot) {
      schedule();
    }
    return {
      lastHeatmapRevision: heatmapSnapshot ? heatmapSnapshot.cacheRevision : (current || {}).lastHeatmapRevision,
      lastHistoryRevision: historySnapshot ? historySnapshot.cacheRevision : (current || {}).lastHistoryRevision,
      lastHistoryFollowRevision: historySnapshot ? historySnapshot.followRevision : (current || {}).lastHistoryFollowRevision,
      heatmapActualFps: fps(root.heatmapSamples),
      historyActualFps: fps(root.historySamples),
      coalescedFrames: root.coalescedFrames,
      droppedFrames: root.droppedFrames,
      frontendRenderSkipped: root.droppedFrames,
      lastClientError: root.lastClientError
    };
  };

  window.SensorArrayLive = root;
})();
