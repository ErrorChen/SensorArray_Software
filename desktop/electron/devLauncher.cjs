const { spawn } = require("node:child_process");
const path = require("node:path");
const electron = require("electron");

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;
env.SENSORARRAY_FRONTEND_URL = env.SENSORARRAY_FRONTEND_URL || "http://127.0.0.1:5173";

const mainPath = path.join(__dirname, "..", "dist-electron", "main.js");
const child = spawn(electron, [mainPath], {
  stdio: "inherit",
  env,
  windowsHide: false
});

child.on("close", (code, signal) => {
  if (code === null) {
    console.error(`${electron} exited with signal ${signal}`);
    process.exit(1);
  }
  process.exit(code);
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    if (!child.killed) {
      child.kill(signal);
    }
  });
}
