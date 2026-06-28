export const defaultBackendHost = "127.0.0.1";
export const defaultBackendPort = 8888;
export const backendFallbackCount = 100;

export function buildBackendPortCandidates(startPort = defaultBackendPort, fallbackCount = backendFallbackCount): number[] {
  return Array.from({ length: fallbackCount + 1 }, (_, index) => startPort + index);
}
