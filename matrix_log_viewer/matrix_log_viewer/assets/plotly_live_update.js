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
    frontendRenderSkipped: 0,
    lastClientError: "",
    heatmapSamples: [],
    historySamples: []
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
    const z = snapshot.matrixDisplay || snapshot.matrixUv || [];
    const unit = snapshot.displayUnit || "uV";
    const tickFormat = snapshot.colorbarTickFormat || ",.3f";
    const xy = selectedXY(snapshot.selectedCell);
    const colorbar = {
      title: { text: snapshot.colorbarTitle || unit },
      tickformat: tickFormat,
      exponentformat: "none",
      separatethousands: true
    };
    const heatmapTrace = {
      type: "heatmap",
      z: z,
      x: labels("D"),
      y: labels("S"),
      text: snapshot.text || [],
      texttemplate: "%{text}",
      textfont: { size: 11, color: "#111827" },
      customdata: snapshot.customdata || [],
      colorscale: "RdYlBu",
      reversescale: true,
      colorbar: colorbar,
      zauto: snapshot.zauto !== false,
      hovertemplate:
        "cell=%{customdata[0]}<br>" +
        "valid=%{customdata[1]}<br>" +
        "value=%{customdata[2]} " + unit + "<br>" +
        "raw=%{customdata[5]} uV<br>" +
        "seq=%{customdata[4]}<br>" +
        "status=%{customdata[6]} %{customdata[7]}<extra></extra>"
    };
    if (snapshot.zauto === false && Number.isFinite(snapshot.zmin) && Number.isFinite(snapshot.zmax)) {
      heatmapTrace.zmin = snapshot.zmin;
      heatmapTrace.zmax = snapshot.zmax;
    }
    const selectionTrace = {
      type: "scatter",
      mode: "markers",
      x: xy.x,
      y: xy.y,
      marker: { symbol: "square-open", size: 62, line: { color: "#111827", width: 3 } },
      hoverinfo: "skip",
      showlegend: false
    };
    const layout = {
      title: "8x8 Matrix",
      margin: { l: 58, r: 24, t: 56, b: 52 },
      paper_bgcolor: "white",
      plot_bgcolor: "white",
      font: { family: "Segoe UI, Arial, sans-serif", size: 12, color: "#17202a" },
      clickmode: "event+select",
      xaxis: { side: "top", constrain: "domain" },
      yaxis: { autorange: "reversed", scaleanchor: "x", scaleratio: 1 },
      uirevision: "heatmap:" + (snapshot.stream || "FAST_BINARY")
    };
    if (!root.heatmapInitialized) {
      Plotly.newPlot(div, [heatmapTrace, selectionTrace], layout, { displayModeBar: false, responsive: true });
      root.heatmapInitialized = true;
      root.heatmapSelectedCell = snapshot.selectedCell;
    } else {
      Plotly.react(div, [heatmapTrace, selectionTrace], layout, { displayModeBar: false, responsive: true });
      root.heatmapSelectedCell = snapshot.selectedCell;
    }
    record(root.heatmapSamples);
  }

  function applyHistory(snapshot) {
    if (!snapshot || snapshot.kind !== "history") {
      return;
    }
    const div = graphDiv("history-graph");
    if (!div || !window.Plotly) {
      return;
    }
    const title = snapshot.title || ("History of " + snapshot.selectedCell + " / " + snapshot.stream);
    const layout = {
      title: title,
      margin: { l: 58, r: 24, t: 56, b: 52 },
      paper_bgcolor: "white",
      plot_bgcolor: "white",
      font: { family: "Segoe UI, Arial, sans-serif", size: 12, color: "#17202a" },
      xaxis: { title: snapshot.xAxis || "timeSeconds", showgrid: true, gridcolor: "#e5e7eb" },
      yaxis: { title: "value (" + (snapshot.unit || "uV") + ")", showgrid: true, gridcolor: "#e5e7eb", autorange: true },
      uirevision: snapshot.key
    };
    if (snapshot.reset || !root.historyInitialized || root.currentHistoryKey !== snapshot.key) {
      Plotly.react(div, [{
        type: "scattergl",
        mode: snapshot.showMarkers ? "lines+markers" : "lines",
        x: snapshot.x || [],
        y: snapshot.y || [],
        name: (snapshot.selectedCell || "-") + " / " + (snapshot.stream || "-"),
        line: { color: "#0f766e", width: 2 },
        marker: { size: 4 },
        hovertemplate: "%{x}<br>%{y}<extra></extra>"
      }], layout, { displayModeBar: true, responsive: true, scrollZoom: true });
      root.historyInitialized = true;
      root.currentHistoryKey = snapshot.key;
    } else if (snapshot.key === root.currentHistoryKey) {
      const x = snapshot.x || [];
      const y = snapshot.y || [];
      if (x.length || y.length) {
        Plotly.extendTraces(div, { x: [x], y: [y] }, [0], snapshot.maxPoints || 1200);
      }
    } else {
      root.frontendRenderSkipped += 1;
      return;
    }
    if (snapshot.followLatest && snapshot.x && snapshot.x.length) {
      const xmax = snapshot.x[snapshot.x.length - 1];
      const xmin = snapshot.x[0];
      if (Number.isFinite(xmin) && Number.isFinite(xmax) && xmax > xmin) {
        Plotly.relayout(div, { "xaxis.range": [xmin, xmax] });
      }
    }
    record(root.historySamples);
  }

  function flush() {
    root.rafScheduled = false;
    const heatmap = root.pendingHeatmapSnapshot;
    const history = root.pendingHistorySnapshot;
    root.pendingHeatmapSnapshot = null;
    root.pendingHistorySnapshot = null;
    try {
      applyHeatmap(heatmap);
      applyHistory(history);
      root.lastClientError = "";
    } catch (error) {
      root.lastClientError = String(error && error.message ? error.message : error);
      console.error("SensorArray live Plotly update failed", error);
    }
  }

  function schedule() {
    if (root.rafScheduled) {
      root.frontendRenderSkipped += 1;
      return;
    }
    root.rafScheduled = true;
    window.requestAnimationFrame(flush);
  }

  root.applySnapshots = function (heatmapSnapshot, historySnapshot, current) {
    if (heatmapSnapshot && heatmapSnapshot.cacheRevision !== (current || {}).lastHeatmapRevision) {
      root.pendingHeatmapSnapshot = heatmapSnapshot;
    }
    if (historySnapshot && historySnapshot.cacheRevision !== (current || {}).lastHistoryRevision) {
      root.pendingHistorySnapshot = historySnapshot;
    }
    if (root.pendingHeatmapSnapshot || root.pendingHistorySnapshot) {
      schedule();
    }
    return {
      lastHeatmapRevision: heatmapSnapshot ? heatmapSnapshot.cacheRevision : (current || {}).lastHeatmapRevision,
      lastHistoryRevision: historySnapshot ? historySnapshot.cacheRevision : (current || {}).lastHistoryRevision,
      heatmapActualFps: fps(root.heatmapSamples),
      historyActualFps: fps(root.historySamples),
      frontendRenderSkipped: root.frontendRenderSkipped,
      lastClientError: root.lastClientError
    };
  };

  window.SensorArrayLive = root;
})();
