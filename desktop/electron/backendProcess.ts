import type { ChildProcessWithoutNullStreams } from "node:child_process";
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import http from "node:http";
import net from "node:net";
import path from "node:path";

import { buildBackendPortCandidates, defaultBackendHost } from "./backendPortPolicy.js";

export type BackendProcess = {
  child: ChildProcessWithoutNullStreams;
  host: string;
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
  host?: string;
  port: number;
  isPackaged: boolean;
  resourcesPath: string;
};

export type BackendStartWithHealthyPortOptions = Omit<BackendStartOptions, "port"> & {
  ports?: number[];
  healthTimeoutMs?: number;
};

type BackendPortFailure = {
  port: number;
  reason: string;
  stdout: string;
  stderr: string;
};

export class BackendStartError extends Error {
  constructor(public readonly failures: BackendPortFailure[]) {
    super(formatBackendStartFailures(failures));
    this.name = "BackendStartError";
  }
}

export async function startBackendWithFirstHealthyPort(options: BackendStartWithHealthyPortOptions): Promise<BackendProcess> {
  const host = options.host ?? defaultBackendHost;
  const ports = options.ports ?? buildBackendPortCandidates();
  const failures: BackendPortFailure[] = [];
  for (const port of ports) {
    const bindCheck = await canBindPort(host, port);
    if (!bindCheck.ok) {
      failures.push({ port, reason: bindCheck.reason, stdout: "", stderr: "" });
      continue;
    }

    let backend: BackendProcess | null = null;
    try {
      backend = startBackend({ ...options, host, port });
      await waitForHealth(backend, options.healthTimeoutMs ?? 15000);
      console.log(`[backend] selected ${backend.url}`);
      return backend;
    } catch (error) {
      const reason = error instanceof Error ? error.message : String(error);
      failures.push({
        port,
        reason,
        stdout: backend?.stdout.join("").slice(-2000) ?? "",
        stderr: backend?.stderr.join("").slice(-4000) ?? ""
      });
      await stopBackendProcess(backend);
    }
  }
  throw new BackendStartError(failures);
}

export function startBackend(options: BackendStartOptions): BackendProcess {
  const host = options.host ?? defaultBackendHost;
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
    host,
    port: options.port,
    url: `http://${host}:${options.port}`,
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
      if (statusCode === 200) {
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

export async function stopBackendProcess(processInfo: BackendProcess | null, timeoutMs = 3000): Promise<void> {
  if (!processInfo || processInfo.exited || processInfo.child.killed) {
    return;
  }
  await new Promise<void>((resolve) => {
    const timeout = setTimeout(() => {
      try {
        processInfo.child.kill("SIGKILL");
      } catch {
        // The process may have exited before the forced kill request.
      }
      resolve();
    }, timeoutMs);
    processInfo.child.once("exit", () => {
      clearTimeout(timeout);
      resolve();
    });
    try {
      processInfo.child.kill();
    } catch {
      clearTimeout(timeout);
      resolve();
    }
  });
}

type BackendCommand = {
  executable: string;
  args: string[];
  cwd: string;
  env: NodeJS.ProcessEnv;
};

function resolveBackendCommand(options: BackendStartOptions): BackendCommand {
  const args = ["--host", options.host ?? defaultBackendHost, "--port", String(options.port)];
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
  const localCandidates =
    process.platform === "win32"
      ? [path.join(projectRoot, ".venv", "Scripts", "python.exe")]
      : [path.join(projectRoot, ".venv", "bin", "python"), path.join(projectRoot, ".venv", "bin", "python3")];
  for (const candidate of localCandidates) {
    if (existsSync(candidate)) {
      return candidate;
    }
  }
  return process.platform === "win32" ? "python" : "python3";
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

export function canBindPort(host: string, port: number): Promise<{ ok: true } | { ok: false; reason: string }> {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once("error", (error: NodeJS.ErrnoException) => resolve({ ok: false, reason: error.code ? `${error.code}: ${error.message}` : error.message }));
    server.once("listening", () => {
      server.close(() => resolve({ ok: true }));
    });
    server.listen(port, host);
  });
}

function formatBackendStartFailures(failures: BackendPortFailure[]): string {
  const lines = failures.map((failure) => {
    const stderr = failure.stderr ? ` stderr=${summarizeLine(failure.stderr)}` : "";
    const stdout = failure.stdout ? ` stdout=${summarizeLine(failure.stdout)}` : "";
    return `${failure.port}: ${failure.reason}${stderr}${stdout}`;
  });
  return `No healthy backend port found from 8888 through 8988.\n${lines.join("\n")}`;
}

function summarizeLine(value: string): string {
  return value.replace(/\s+/g, " ").trim().slice(-700);
}
