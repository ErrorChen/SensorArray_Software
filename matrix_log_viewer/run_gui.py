from __future__ import annotations

import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    from serial.tools import list_ports
except Exception:  # pragma: no cover - pyserial may not be installed yet.
    list_ports = None


class ViewerLauncher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Matrix Log Viewer Launcher")
        self.geometry("860x620")
        self.minsize(760, 560)

        self.project_dir = Path(__file__).resolve().parent
        self.process: subprocess.Popen | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()

        self.input_mode = tk.StringVar(value="serial")
        self.port = tk.StringVar(value="")
        self.baud = tk.StringVar(value="115200")
        self.max_points = tk.StringVar(value="5000")
        self.save_csv = tk.StringVar(value="")
        self.replay_file = tk.StringVar(value="")
        self.replay_speed = tk.StringVar(value="10")
        self.host = tk.StringVar(value="127.0.0.1")
        self.port_web = tk.StringVar(value="8050")
        self.debug = tk.BooleanVar(value=False)

        self._build_ui()
        self._refresh_ports()
        self._poll_log_queue()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=14)
        root.grid(row=0, column=0, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(5, weight=1)

        title = ttk.Label(root, text="Matrix Log Viewer", font=("Segoe UI", 18, "bold"))
        title.grid(row=0, column=0, sticky="w", pady=(0, 12))

        source_frame = ttk.LabelFrame(root, text="Input Source", padding=12)
        source_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        for index in range(6):
            source_frame.columnconfigure(index, weight=1 if index in (1, 4) else 0)

        ttk.Radiobutton(
            source_frame,
            text="Serial COM",
            variable=self.input_mode,
            value="serial",
            command=self._sync_mode_state,
        ).grid(row=0, column=0, sticky="w")
        self.port_combo = ttk.Combobox(source_frame, textvariable=self.port, width=22)
        self.port_combo.grid(row=0, column=1, sticky="ew", padx=(8, 8))
        ttk.Button(source_frame, text="Refresh Ports", command=self._refresh_ports).grid(
            row=0, column=2, sticky="w"
        )

        ttk.Label(source_frame, text="Baud").grid(row=0, column=3, sticky="e", padx=(16, 6))
        self.baud_entry = ttk.Entry(source_frame, textvariable=self.baud, width=10)
        self.baud_entry.grid(row=0, column=4, sticky="w")

        ttk.Radiobutton(
            source_frame,
            text="Replay File",
            variable=self.input_mode,
            value="replay",
            command=self._sync_mode_state,
        ).grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.replay_entry = ttk.Entry(source_frame, textvariable=self.replay_file)
        self.replay_entry.grid(row=1, column=1, columnspan=4, sticky="ew", padx=(8, 8), pady=(10, 0))
        ttk.Button(source_frame, text="Browse", command=self._browse_replay_file).grid(
            row=1, column=5, sticky="e", pady=(10, 0)
        )

        ttk.Label(source_frame, text="Replay speed").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.replay_speed_entry = ttk.Entry(source_frame, textvariable=self.replay_speed, width=10)
        self.replay_speed_entry.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=(10, 0))

        settings_frame = ttk.LabelFrame(root, text="Viewer Settings", padding=12)
        settings_frame.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        for index in range(6):
            settings_frame.columnconfigure(index, weight=1 if index in (1, 3, 5) else 0)

        ttk.Label(settings_frame, text="Max points/cell").grid(row=0, column=0, sticky="w")
        ttk.Entry(settings_frame, textvariable=self.max_points, width=10).grid(
            row=0, column=1, sticky="w", padx=(8, 18)
        )
        ttk.Label(settings_frame, text="Host").grid(row=0, column=2, sticky="w")
        ttk.Entry(settings_frame, textvariable=self.host, width=14).grid(
            row=0, column=3, sticky="w", padx=(8, 18)
        )
        ttk.Label(settings_frame, text="Web port").grid(row=0, column=4, sticky="w")
        ttk.Entry(settings_frame, textvariable=self.port_web, width=10).grid(
            row=0, column=5, sticky="w", padx=(8, 0)
        )

        ttk.Label(settings_frame, text="Append CSV").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(settings_frame, textvariable=self.save_csv).grid(
            row=1, column=1, columnspan=4, sticky="ew", padx=(8, 8), pady=(10, 0)
        )
        ttk.Button(settings_frame, text="Browse", command=self._browse_save_csv).grid(
            row=1, column=5, sticky="e", pady=(10, 0)
        )
        ttk.Checkbutton(settings_frame, text="Debug logging", variable=self.debug).grid(
            row=2, column=0, sticky="w", pady=(10, 0)
        )

        action_frame = ttk.Frame(root)
        action_frame.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        action_frame.columnconfigure(3, weight=1)

        self.start_button = ttk.Button(action_frame, text="Start Viewer", command=self._start_viewer)
        self.start_button.grid(row=0, column=0, sticky="w")
        self.stop_button = ttk.Button(action_frame, text="Stop Viewer", command=self._stop_viewer, state="disabled")
        self.stop_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.browser_button = ttk.Button(
            action_frame,
            text="Open Browser",
            command=self._open_browser,
            state="disabled",
        )
        self.browser_button.grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.status_label = ttk.Label(action_frame, text="Stopped")
        self.status_label.grid(row=0, column=3, sticky="e")

        ttk.Label(root, text="Runtime Log").grid(row=4, column=0, sticky="w")
        log_frame = ttk.Frame(root)
        log_frame.grid(row=5, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        self.log_text = tk.Text(log_frame, height=14, wrap="word", state="disabled")
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scroll.set)

        self._sync_mode_state()

    def _refresh_ports(self) -> None:
        ports = []
        if list_ports is not None:
            ports = [port.device for port in list_ports.comports()]

        self.port_combo["values"] = ports
        if ports and not self.port.get():
            self.port.set(ports[0])

    def _sync_mode_state(self) -> None:
        serial_state = "normal" if self.input_mode.get() == "serial" else "disabled"
        replay_state = "normal" if self.input_mode.get() == "replay" else "disabled"

        self.port_combo.configure(state=serial_state)
        self.baud_entry.configure(state=serial_state)
        self.replay_entry.configure(state=replay_state)
        self.replay_speed_entry.configure(state=replay_state)

    def _browse_replay_file(self) -> None:
        filename = filedialog.askopenfilename(
            title="Choose MATV log file",
            initialdir=self.project_dir,
            filetypes=[("Log files", "*.log *.txt *.csv"), ("All files", "*.*")],
        )
        if filename:
            self.replay_file.set(filename)
            self.input_mode.set("replay")
            self._sync_mode_state()

    def _browse_save_csv(self) -> None:
        filename = filedialog.asksaveasfilename(
            title="Append parsed frames to CSV",
            initialdir=self.project_dir,
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
        )
        if filename:
            self.save_csv.set(filename)

    def _start_viewer(self) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showinfo("Viewer is running", "Matrix Log Viewer is already running.")
            return

        try:
            args = self._build_args()
        except ValueError as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return

        self._append_log("> " + " ".join(args))
        try:
            self.process = subprocess.Popen(
                args,
                cwd=self.project_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception as exc:
            messagebox.showerror("Failed to start", str(exc))
            self.process = None
            return

        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.browser_button.configure(state="normal")
        self.status_label.configure(text="Starting")

        threading.Thread(target=self._read_process_output, daemon=True).start()
        threading.Thread(target=self._open_browser_after_delay, daemon=True).start()

    def _build_args(self) -> list[str]:
        args = [sys.executable, str(self.project_dir / "run_viewer.py")]

        max_points = self._positive_int(self.max_points.get(), "--max-points")
        web_port = self._positive_int(self.port_web.get(), "--port-web")
        args.extend(["--max-points", str(max_points), "--host", self.host.get().strip() or "127.0.0.1"])
        args.extend(["--port-web", str(web_port)])

        if self.input_mode.get() == "replay":
            replay_file = self.replay_file.get().strip()
            if not replay_file:
                raise ValueError("Please choose a replay log file.")
            if not Path(replay_file).exists():
                raise ValueError(f"Replay file does not exist: {replay_file}")
            replay_speed = self._positive_float(self.replay_speed.get(), "--replay-speed")
            args.extend(["--replay-file", replay_file, "--replay-speed", str(replay_speed)])
        else:
            port = self.port.get().strip()
            if not port:
                raise ValueError("Please enter or choose a serial COM port.")
            baud = self._positive_int(self.baud.get(), "--baud")
            args.extend(["--port", port, "--baud", str(baud)])

        save_csv = self.save_csv.get().strip()
        if save_csv:
            args.extend(["--save-csv", save_csv])

        if self.debug.get():
            args.append("--debug")

        return args

    @staticmethod
    def _positive_int(value: str, name: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer.") from exc
        if parsed <= 0:
            raise ValueError(f"{name} must be greater than 0.")
        return parsed

    @staticmethod
    def _positive_float(value: str, name: str) -> float:
        try:
            parsed = float(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be a number.") from exc
        if parsed <= 0:
            raise ValueError(f"{name} must be greater than 0.")
        return parsed

    def _read_process_output(self) -> None:
        assert self.process is not None
        if self.process.stdout is not None:
            for line in self.process.stdout:
                self.log_queue.put(line.rstrip())

        return_code = self.process.wait()
        self.log_queue.put(f"Viewer process exited with code {return_code}.")
        self.after(0, self._mark_stopped)

    def _open_browser_after_delay(self) -> None:
        time.sleep(1.2)
        if self.process and self.process.poll() is None:
            self._open_browser()

    def _open_browser(self) -> None:
        url = f"http://{self.host.get().strip() or '127.0.0.1'}:{self.port_web.get().strip() or '8050'}"
        webbrowser.open(url)
        self.status_label.configure(text=f"Running: {url}")

    def _stop_viewer(self) -> None:
        if not self.process or self.process.poll() is not None:
            self._mark_stopped()
            return

        self._append_log("Stopping viewer...")
        self.process.terminate()
        try:
            self.process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
        self._mark_stopped()

    def _mark_stopped(self) -> None:
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.browser_button.configure(state="disabled")
        self.status_label.configure(text="Stopped")

    def _poll_log_queue(self) -> None:
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(line)
        self.after(120, self._poll_log_queue)

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        self._stop_viewer()
        self.destroy()


def main() -> int:
    app = ViewerLauncher()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

