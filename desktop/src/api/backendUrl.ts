const defaultBackendHost = "127.0.0.1";
const defaultBackendPort = 8888;
const backendFallbackCount = 100;

export function buildBackendUrlCandidates(startPort = defaultBackendPort, fallbackCount = backendFallbackCount): string[] {
  return Array.from({ length: fallbackCount + 1 }, (_, index) => `http://${defaultBackendHost}:${startPort + index}`);
}

export async function resolveBackendUrl(): Promise<string> {
  const params = new URLSearchParams(window.location.search);
  const queryUrl = params.get("backendUrl");
  if (queryUrl) {
    return normaliseBackendUrl(queryUrl);
  }
  if (window.sensorarrayDesktop?.getBackendUrl) {
    return normaliseBackendUrl(await window.sensorarrayDesktop.getBackendUrl());
  }
  for (const candidate of buildBackendUrlCandidates()) {
    if (await healthOk(candidate)) {
      return candidate;
    }
  }
  throw new Error("Backend unavailable at http://127.0.0.1:8888-8988");
}

export function normaliseBackendUrl(value: string): string {
  const trimmed = String(value || "").trim().replace(/\/+$/, "");
  if (!trimmed) {
    throw new Error("Backend URL is not resolved");
  }
  const parsed = new URL(trimmed);
  if (parsed.port === "6666") {
    throw new Error("Legacy unsafe backend port 6666 is still in use; check backend port policy.");
  }
  return parsed.toString().replace(/\/+$/, "");
}

async function healthOk(baseUrl: string): Promise<boolean> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), 700);
  try {
    const response = await fetch(`${baseUrl}/health`, { signal: controller.signal, cache: "no-store" });
    return response.status === 200;
  } catch {
    return false;
  } finally {
    window.clearTimeout(timeout);
  }
}
