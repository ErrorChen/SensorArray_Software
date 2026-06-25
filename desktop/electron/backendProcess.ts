import { ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import net from "node:net";
import path from "node:path";

export type BackendProcess = {
  child: ChildProcessWithoutNullStreams;
  port: number;
  url: string;
  stderr: string[];
};

export async function findAvailablePort(startPort: number): Promise<number> {
  for (let port = startPort; port < startPort + 50; port += 1) {
    if (await canListen(port)) {
      return port;
    }
  }
  throw new Error(`No available backend port from ${startPort}`);
}

export function startBackend(projectRoot: string, port: number): BackendProcess {
  const python = resolvePython(projectRoot);
  const args = ["-m", "sensorarray_backend", "--host", "127.0.0.1", "--port", String(port)];
  const child = spawn(python, args, {
    cwd: projectRoot,
    windowsHide: true,
    env: {
      ...process.env,
      PYTHONPATH: path.join(projectRoot, "src")
    }
  });
  const stderr: string[] = [];
  child.stderr.on("data", (chunk: Buffer) => {
    stderr.push(chunk.toString("utf8"));
    while (stderr.length > 120) {
      stderr.shift();
    }
  });
  return {
    child,
    port,
    url: `http://127.0.0.1:${port}`,
    stderr
  };
}

export async function waitForHealth(url: string, timeoutMs = 15000): Promise<void> {
  const stopTime = Date.now() + timeoutMs;
  let lastError = "";
  while (Date.now() < stopTime) {
    try {
      const response = await fetch(`${url}/health`);
      if (response.ok) {
        return;
      }
      lastError = `HTTP ${response.status}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`Backend did not become healthy: ${lastError}`);
}

export function stopBackend(processInfo: BackendProcess | null): void {
  if (!processInfo || processInfo.child.killed) {
    return;
  }
  processInfo.child.kill();
}

function resolvePython(projectRoot: string): string {
  const localPython = path.join(projectRoot, ".venv", "Scripts", "python.exe");
  if (existsSync(localPython)) {
    return localPython;
  }
  return "python";
}

function canListen(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once("error", () => resolve(false));
    server.once("listening", () => {
      server.close(() => resolve(true));
    });
    server.listen(port, "127.0.0.1");
  });
}
