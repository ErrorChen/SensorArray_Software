import type { ChildProcessWithoutNullStreams } from "node:child_process";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import http from "node:http";
import net from "node:net";
import path from "node:path";

export type BackendProcess = {
  child: ChildProcessWithoutNullStreams;
  port: number;
  url: string;
  stdout: string[];
  stderr: string[];
  exited: boolean;
  exitCode: number | null;
  exitSignal: NodeJS.Signals | null;
  spawnError: Error | null;
};

export type BackendStartOptions = {
  projectRoot: string;
  port: number;
  isPackaged: boolean;
  resourcesPath: string;
};

export async function findAvailablePort(preferredPorts: number[] = [6666, 8888, ...Array.from({ length: 50 }, (_, index) => 8750 + index)]): Promise<number> {
  for (const port of preferredPorts) {
    if (await canListen(port)) {
      return port;
    }
  }
  throw new Error(`No available backend port from ${preferredPorts.join(", ")}`);
}

export function startBackend(options: BackendStartOptions): BackendProcess {
  const command = resolveBackendCommand(options);
  const child = spawn(command.executable, command.args, {
    cwd: command.cwd,
    windowsHide: true,
    env: command.env
  });
  const stdout: string[] = [];
  const stderr: string[] = [];
  const backend: BackendProcess = {
    child,
    port: options.port,
    url: `http://127.0.0.1:${options.port}`,
    stdout,
    stderr,
    exited: false,
    exitCode: null,
    exitSignal: null,
    spawnError: null
  };
  child.stdout.on("data", (chunk: Buffer) => appendProcessLog(stdout, chunk.toString("utf8")));
  child.stderr.on("data", (chunk: Buffer) => appendProcessLog(stderr, chunk.toString("utf8")));
  child.once("error", (error) => {
    backend.spawnError = error;
    appendProcessLog(stderr, `${error.message}\n`);
  });
  child.once("exit", (code, signal) => {
    backend.exited = true;
    backend.exitCode = code;
    backend.exitSignal = signal;
  });
  return backend;
}

export async function waitForHealth(processInfo: BackendProcess, timeoutMs = 15000): Promise<void> {
  const stopTime = Date.now() + timeoutMs;
  let lastError = "";
  while (Date.now() < stopTime) {
    if (processInfo.spawnError) {
      throw new Error(`Backend failed to start: ${processInfo.spawnError.message}`);
    }
    if (processInfo.exited) {
      throw new Error(formatBackendExit(processInfo));
    }
    try {
      const statusCode = await requestHealthStatus(`${processInfo.url}/health`);
      if (statusCode >= 200 && statusCode < 300) {
        return;
      }
      lastError = `HTTP ${statusCode}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  throw new Error(`Backend did not become healthy: ${lastError}`);
}

export function stopBackend(processInfo: BackendProcess | null): void {
  if (!processInfo || processInfo.exited || processInfo.child.killed) {
    return;
  }
  try {
    processInfo.child.kill();
  } catch {
    // The process may have exited between the state check and kill request.
  }
}

type BackendCommand = {
  executable: string;
  args: string[];
  cwd: string;
  env: NodeJS.ProcessEnv;
};

function resolveBackendCommand(options: BackendStartOptions): BackendCommand {
  const args = ["--host", "127.0.0.1", "--port", String(options.port)];
  if (options.isPackaged) {
    const backendExe = path.join(options.resourcesPath, "backend", "SensorArrayBackend.exe");
    if (!existsSync(backendExe)) {
      throw new Error(`Packaged backend executable was not found: ${backendExe}`);
    }
    return {
      executable: backendExe,
      args,
      cwd: path.dirname(backendExe),
      env: { ...process.env }
    };
  }
  return {
    executable: resolvePython(options.projectRoot),
    args: ["-m", "sensorarray_backend", ...args],
    cwd: options.projectRoot,
    env: {
      ...process.env,
      PYTHONPATH: buildPythonPath(options.projectRoot)
    }
  };
}

function resolvePython(projectRoot: string): string {
  const localPython = path.join(projectRoot, ".venv", "Scripts", "python.exe");
  if (existsSync(localPython)) {
    return localPython;
  }
  return "python";
}

function buildPythonPath(projectRoot: string): string {
  const srcPath = path.join(projectRoot, "src");
  return process.env.PYTHONPATH ? `${srcPath}${path.delimiter}${process.env.PYTHONPATH}` : srcPath;
}

function requestHealthStatus(url: string): Promise<number> {
  return new Promise((resolve, reject) => {
    const request = http.get(url, { timeout: 1000 }, (response) => {
      response.resume();
      response.once("end", () => resolve(response.statusCode ?? 0));
    });
    request.once("timeout", () => request.destroy(new Error("health request timed out")));
    request.once("error", reject);
  });
}

function appendProcessLog(log: string[], value: string): void {
  log.push(value);
  while (log.length > 160) {
    log.shift();
  }
}

function formatBackendExit(processInfo: BackendProcess): string {
  const code = processInfo.exitCode === null ? "null" : String(processInfo.exitCode);
  const signal = processInfo.exitSignal ?? "null";
  return `Backend exited before becoming healthy (code=${code}, signal=${signal})`;
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
