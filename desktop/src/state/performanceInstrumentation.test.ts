import { describe, expect, it } from "vitest";

import {
  frontendPerformanceSnapshot,
  recordBackendSnapshot,
  recordCoalescedSnapshot,
  recordPresentedFrame,
  recordRenderDuration,
  registerChartInstance,
  registerResizeObserver,
  resetFrontendPerformanceInstrumentation,
  setHistoryPointCount,
  setWebSocketConnected
} from "./performanceInstrumentation";

describe("frontend performance instrumentation", () => {
  it("accounts resources, presentation coalescing, long frames, and history", () => {
    resetFrontendPerformanceInstrumentation();
    const disposeChart = registerChartInstance();
    const disposeObserver = registerResizeObserver();
    setWebSocketConnected(true);
    recordBackendSnapshot(false);
    recordBackendSnapshot(true);
    recordCoalescedSnapshot();
    recordPresentedFrame(1000);
    recordPresentedFrame(1040);
    recordRenderDuration(55);
    setHistoryPointCount(321);

    expect(frontendPerformanceSnapshot(1080)).toMatchObject({
      activeCharts: 1,
      activeResizeObservers: 1,
      activeWebSockets: 1,
      backendSnapshots: 2,
      presentedFrames: 2,
      coalescedSnapshots: 1,
      hiddenSnapshots: 1,
      longFrameWarnings: 1,
      historyPoints: 321
    });
    disposeChart();
    disposeChart();
    disposeObserver();
    setWebSocketConnected(false);
    expect(frontendPerformanceSnapshot(1080)).toMatchObject({
      activeCharts: 0,
      activeResizeObservers: 0,
      activeWebSockets: 0
    });
  });
});
