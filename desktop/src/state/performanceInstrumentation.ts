export type FrontendPerformanceSnapshot = {
  activeCharts: number;
  activeResizeObservers: number;
  activeWebSockets: number;
  backendSnapshots: number;
  presentedFrames: number;
  coalescedSnapshots: number;
  hiddenSnapshots: number;
  presentationRateHz: number;
  latestRenderMs: number | null;
  maximumRenderMs: number | null;
  longFrameWarnings: number;
  historyPoints: number;
  visibility: "visible" | "hidden" | "unknown";
  rendererHeapBytes: number | null;
};

const state = {
  activeCharts: 0,
  activeResizeObservers: 0,
  activeWebSockets: 0,
  backendSnapshots: 0,
  presentedFrames: 0,
  coalescedSnapshots: 0,
  hiddenSnapshots: 0,
  latestRenderMs: null as number | null,
  maximumRenderMs: null as number | null,
  longFrameWarnings: 0,
  historyPoints: 0,
  presentationTimes: [] as number[]
};

export function registerChartInstance(): () => void {
  state.activeCharts += 1;
  return once(() => {
    state.activeCharts = Math.max(0, state.activeCharts - 1);
  });
}

export function registerResizeObserver(): () => void {
  state.activeResizeObservers += 1;
  return once(() => {
    state.activeResizeObservers = Math.max(0, state.activeResizeObservers - 1);
  });
}

export function setWebSocketConnected(connected: boolean): void {
  state.activeWebSockets = connected ? 1 : 0;
}

export function recordBackendSnapshot(hidden: boolean): void {
  state.backendSnapshots += 1;
  if (hidden) {
    state.hiddenSnapshots += 1;
  }
}

export function recordCoalescedSnapshot(): void {
  state.coalescedSnapshots += 1;
}

export function recordPresentedFrame(now = monotonicNow()): void {
  state.presentedFrames += 1;
  state.presentationTimes.push(now);
  prunePresentationTimes(now);
}

export function recordRenderDuration(durationMs: number): void {
  if (!Number.isFinite(durationMs) || durationMs < 0) {
    return;
  }
  state.latestRenderMs = durationMs;
  state.maximumRenderMs = Math.max(state.maximumRenderMs ?? 0, durationMs);
  if (durationMs > 50) {
    state.longFrameWarnings += 1;
  }
}

export function setHistoryPointCount(points: number): void {
  state.historyPoints = Math.max(0, Math.trunc(points));
}

export function frontendPerformanceSnapshot(now = monotonicNow()): FrontendPerformanceSnapshot {
  prunePresentationTimes(now);
  const oldest = state.presentationTimes[0];
  const spanSeconds = oldest === undefined ? 0 : Math.max(0.001, (now - oldest) / 1000);
  const rate = state.presentationTimes.length < 2 ? state.presentationTimes.length : (state.presentationTimes.length - 1) / spanSeconds;
  return {
    activeCharts: state.activeCharts,
    activeResizeObservers: state.activeResizeObservers,
    activeWebSockets: state.activeWebSockets,
    backendSnapshots: state.backendSnapshots,
    presentedFrames: state.presentedFrames,
    coalescedSnapshots: state.coalescedSnapshots,
    hiddenSnapshots: state.hiddenSnapshots,
    presentationRateHz: Math.round(rate * 10) / 10,
    latestRenderMs: state.latestRenderMs,
    maximumRenderMs: state.maximumRenderMs,
    longFrameWarnings: state.longFrameWarnings,
    historyPoints: state.historyPoints,
    visibility: typeof document === "undefined" ? "unknown" : document.hidden ? "hidden" : "visible",
    rendererHeapBytes: rendererHeapBytes()
  };
}

export function resetFrontendPerformanceInstrumentation(): void {
  state.activeCharts = 0;
  state.activeResizeObservers = 0;
  state.activeWebSockets = 0;
  state.backendSnapshots = 0;
  state.presentedFrames = 0;
  state.coalescedSnapshots = 0;
  state.hiddenSnapshots = 0;
  state.latestRenderMs = null;
  state.maximumRenderMs = null;
  state.longFrameWarnings = 0;
  state.historyPoints = 0;
  state.presentationTimes = [];
}

function prunePresentationTimes(now: number): void {
  const cutoff = now - 2000;
  while (state.presentationTimes.length && state.presentationTimes[0] < cutoff) {
    state.presentationTimes.shift();
  }
}

function monotonicNow(): number {
  return typeof performance === "undefined" ? Date.now() : performance.now();
}

function rendererHeapBytes(): number | null {
  if (typeof performance === "undefined") {
    return null;
  }
  const memory = (performance as Performance & { memory?: { usedJSHeapSize?: number } }).memory;
  return typeof memory?.usedJSHeapSize === "number" ? memory.usedJSHeapSize : null;
}

function once(action: () => void): () => void {
  let complete = false;
  return () => {
    if (complete) return;
    complete = true;
    action();
  };
}
