import type { LogRow } from "../api/types";

export type StatusSeverity = "ok" | "warn" | "error" | "info";

export type StatusCategory =
  | "Transport"
  | "BLE"
  | "Wi-Fi"
  | "Serial"
  | "Parser"
  | "Rows"
  | "Measurement"
  | "ADS"
  | "Baseline"
  | "Display"
  | "Battery"
  | "I2C"
  | "Firmware"
  | "Error"
  | "Other";

export type LogFieldValue = string | number | boolean | null;

export type LogTagSchema = {
  category: StatusCategory;
  title: string;
  explanation: string;
  fieldLabels: Record<string, string>;
  fieldFormatters?: Record<string, (value: LogFieldValue) => string>;
  severity?: StatusSeverity | ((row: LogRow, details: Record<string, LogFieldValue>) => StatusSeverity);
};

const commonFields: Record<string, string> = {
  conn: "Connection state (conn)",
  sub: "Notify subscription count (sub)",
  mtu: "Negotiated MTU bytes (mtu)",
  phy: "BLE PHY TX/RX (phy)",
  mq: "Message queue depth (mq)",
  mode: "Mode (mode)",
  ok: "OK count (ok)",
  nack: "NACK count (nack)",
  to: "Timeout count (to)",
  rec: "Received count (rec)",
  freq: "Frequency (freq)",
  bus: "I2C bus (bus)",
  ms: "Milliseconds (ms)",
  md: "Mode detail (md)",
  fs: "Frame start (fs)",
  fe: "Frame end (fe)",
  cg: "Current generation (cg)",
  tiny: "Tiny packet count (tiny)",
  p: "Primary count (p)",
  a: "Accepted count (a)",
  rv: "Rail voltage valid (rv)",
  ed: "Error detail (ed)"
};

export const logTagSchemas: Record<string, LogTagSchema> = {
  BL50: {
    category: "BLE",
    title: "Bluetooth 50-frame summary",
    explanation: "Firmware BLE connection, notify, MTU, PHY, and queue summary.",
    fieldLabels: commonFields,
    severity: countersSeverity(["nack", "to", "err", "drop"])
  },
  I2C50: {
    category: "I2C",
    title: "I2C 50-frame bus summary",
    explanation: "Firmware I2C bus timing, success, timeout, and error counters.",
    fieldLabels: commonFields,
    severity: countersSeverity(["nack", "to", "err", "fail"])
  },
  SF50: {
    category: "Parser",
    title: "Sensor frame 50-frame summary",
    explanation: "Firmware frame rate, row count, bad frame, drop, and queue summary.",
    fieldLabels: {
      ...commonFields,
      seq: "Sequence range (seq)",
      n: "Frame count (n)",
      rows: "Active row count (rows)",
      cfps: "Core capture FPS (cfps)",
      efps: "Emission FPS (efps)",
      ofps: "Output FPS detail (ofps)",
      bad: "Bad stale/mixed/invalid count (bad)",
      drop: "Dropped frame counts (drop)",
      q: "Queue depth pair (q)"
    },
    severity: countersSeverity(["bad", "drop"])
  },
  TR50: {
    category: "Transport",
    title: "Transport runtime timing summary",
    explanation: "Firmware row timing and acquisition/output runtime counters.",
    fieldLabels: {
      ...commonFields,
      r: "Active rows (r)",
      fu: "Frame update microseconds (fu)",
      rau: "Read average microseconds (rau)",
      rmu: "Read maximum microseconds (rmu)",
      rt: "Read total count (rt)",
      wt: "Write time microseconds (wt)",
      rp: "Read pending count (rp)",
      rs: "Read served count (rs)",
      co: "Core output microseconds (co)",
      ag: "Acquisition group count (ag)",
      agn: "Acquisition normal count (agn)",
      ags: "Acquisition skipped count (ags)",
      agf: "Acquisition failed count (agf)"
    },
    severity: countersSeverity(["agf"])
  },
  AB50: batterySchema("ADS/battery 50-frame summary"),
  ABAT: batterySchema("Battery measurement accepted"),
  OT50: {
    category: "Transport",
    title: "Output transport 50-frame summary",
    explanation: "Firmware output transport and queue summary.",
    fieldLabels: commonFields,
    severity: countersSeverity(["drop", "fail", "err"])
  },
  ROW50: {
    category: "Rows",
    title: "Rows 50-frame summary",
    explanation: "Firmware row command and applied-row summary.",
    fieldLabels: commonFields,
    severity: countersSeverity(["nack", "to", "fail"])
  },
  FB50: {
    category: "Parser",
    title: "Frame buffer 50-frame summary",
    explanation: "Firmware frame-buffer availability and drop summary.",
    fieldLabels: commonFields,
    severity: countersSeverity(["drop", "fail", "err"])
  },
  P50: {
    category: "Parser",
    title: "Parser 50-frame summary",
    explanation: "Firmware parser frame counters.",
    fieldLabels: commonFields,
    severity: countersSeverity(["err", "fail", "bad"])
  },
  H50: {
    category: "Firmware",
    title: "Host 50-frame summary",
    explanation: "Firmware host-loop summary.",
    fieldLabels: commonFields,
    severity: countersSeverity(["err", "fail", "drop"])
  },
  HC: {
    category: "Firmware",
    title: "Host command summary",
    explanation: "Firmware host command status.",
    fieldLabels: commonFields,
    severity: countersSeverity(["err", "fail", "nack"])
  },
  BATD: batterySchema("Battery detail"),
  ARL: batterySchema("Analog rail reading"),
  ADS: {
    category: "ADS",
    title: "ADS converter identity",
    explanation: "ADS126x converter identity and revision information.",
    fieldLabels: {
      chip: "ADS chip model (chip)",
      dev: "Device ID (dev)",
      rev: "Revision (rev)",
      adc: "ADC label (adc)",
      valid: "Identity valid (valid)",
      id: "Request ID (id)"
    }
  },
  ADSCHK: {
    category: "ADS",
    title: "ADS diagnostic check",
    explanation: "Firmware ADS diagnostic sampling progress and result.",
    fieldLabels: {
      id: "Request ID (id)",
      state: "Check state (state)",
      samples: "Sample count (samples)",
      fresh: "Fresh sample count (fresh)",
      period: "Sample period (period)",
      spi: "SPI result (spi)",
      drdy: "DRDY result (drdy)",
      error: "Error detail (error)",
      restore: "Restore result (restore)"
    },
    severity: diagnosticSeverity
  },
  ADSCHKSTAT: {
    category: "ADS",
    title: "ADS diagnostic status",
    explanation: "Firmware ADS diagnostic status associated with its request ID.",
    fieldLabels: {
      id: "Request ID (id)",
      state: "Check state (state)",
      samples: "Sample count (samples)",
      fresh: "Fresh sample count (fresh)",
      period: "Sample period (period)",
      spi: "SPI result (spi)",
      drdy: "DRDY result (drdy)",
      error: "Error detail (error)",
      restore: "Restore result (restore)"
    },
    severity: diagnosticSeverity
  },
  RST: {
    category: "Firmware",
    title: "Firmware reset",
    explanation: "Firmware reset reason reported by the device.",
    fieldLabels: { reason: "Reset reason (reason)" },
    severity: "warn"
  },
  RCMD: {
    category: "Rows",
    title: "Rows command accepted",
    explanation: "Firmware accepted a ROWS request; application still waits for the frame-boundary apply event.",
    fieldLabels: {
      id: "Command ID (id)",
      old: "Previous row count (old)",
      req: "Requested row count (req)",
      status: "Command status (status)",
      generation: "Firmware generation (generation)"
    },
    severity: "ok"
  },
  RAPP: {
    category: "Rows",
    title: "Rows applied",
    explanation: "Firmware applied the requested row count at a frame boundary.",
    fieldLabels: {
      id: "Command ID (id)",
      seq: "Frame sequence (seq)",
      old: "Previous row count (old)",
      new: "New row count (new)",
      gen: "Firmware generation (gen)",
      status: "Apply status (status)"
    },
    severity: "ok"
  },
  MACK: {
    category: "Measurement",
    title: "Measurement mode accepted",
    explanation: "Firmware accepted a mode request; the applied mode remains unchanged until the matching MAPP event.",
    fieldLabels: {
      id: "Request ID (id)",
      old: "Applied mode before request (old)",
      new: "Requested mode (new)",
      state: "Transaction state (state)"
    },
    severity: "ok"
  },
  MAPP: {
    category: "Measurement",
    title: "Measurement mode applied",
    explanation: "Firmware applied a mode request at a frame boundary.",
    fieldLabels: {
      id: "Request ID (id)",
      gen: "Measurement generation (gen)",
      old: "Previous mode (old)",
      new: "Applied mode (new)",
      seq: "First frame sequence (seq)",
      state: "Transaction state (state)",
      transitionUs: "Transition time microseconds (transitionUs)"
    },
    severity: "ok"
  },
  MERR: {
    category: "Measurement",
    title: "Measurement mode failed",
    explanation: "Firmware rejected or failed a mode transition and may have entered SAFE/DEGRADED state.",
    fieldLabels: {
      id: "Request ID (id)", old: "Previous mode (old)", new: "Requested mode (new)",
      seq: "Frame sequence (seq)", state: "Device state (state)", err: "Firmware error (err)"
    },
    severity: "error"
  },
  MFAULT: {
    category: "Measurement",
    title: "Measurement runtime fault",
    explanation: "Firmware reported a measurement fault or restore failure.",
    fieldLabels: { id: "Request ID (id)", state: "Device state (state)", err: "Firmware error (err)", restore: "Restore result (restore)" },
    severity: "error"
  },
  RACK: {
    category: "Measurement",
    title: "Voltage rail configuration accepted",
    explanation: "Firmware accepted measured external AVDD/AVSS; the host still waits for the matching rail RAPP.",
    fieldLabels: { id: "Request ID (id)", avdd: "Measured AVDD µV (avdd)", avss: "Measured AVSS µV (avss)", source: "Rail source (source)", state: "Transaction state (state)" },
    severity: "ok"
  },
  RERR: {
    category: "Measurement",
    title: "Voltage rail configuration failed",
    explanation: "Firmware rejected or failed the external rail configuration.",
    fieldLabels: { id: "Request ID (id)", avdd: "Measured AVDD µV (avdd)", avss: "Measured AVSS µV (avss)", state: "Transaction state (state)", err: "Firmware error (err)" },
    severity: "error"
  },
  BAPP: {
    category: "Battery",
    title: "Battery command completed",
    explanation: "Firmware completed BATNOW/BATD work; BAT? obtains the current telemetry snapshot.",
    fieldLabels: { id: "Request ID (id)", cmd: "Battery command (cmd)", seq: "Frame sequence (seq)", durationUs: "Duration µs (durationUs)", status: "Completion state (status)", err: "Firmware error (err)" },
    severity: diagnosticSeverity
  },
  BATPERIOD: {
    category: "Battery",
    title: "Battery schedule",
    explanation: "Firmware battery telemetry scheduler configuration or live status.",
    fieldLabels: { id: "Request ID (id)", enabled: "Scheduler enabled (enabled)", periodMs: "Period ms (periodMs)", due: "Measurement due (due)", ageMs: "Sample age ms (ageMs)", status: "Transaction state (status)" },
    severity: diagnosticSeverity
  },
  ACK: {
    category: "Transport",
    title: "Firmware ACK",
    explanation: "Firmware acknowledged a host command.",
    fieldLabels: { cmd: "Acknowledged command (cmd)", ...commonFields },
    severity: "ok"
  },
  ERR: {
    category: "Error",
    title: "Firmware error",
    explanation: "Firmware reported an error.",
    fieldLabels: { ...commonFields, msg: "Error message (msg)", error: "Error detail (error)" },
    severity: "error"
  },
  CMD_TX: {
    category: "Transport",
    title: "Command sent",
    explanation: "A command was written through the active backend transport.",
    fieldLabels: {
      mode: "Transport mode (mode)",
      bytes: "Bytes written (bytes)",
      ending: "Command line ending (ending)"
    },
    severity: "ok"
  },
  CMD_TX_FAIL: {
    category: "Error",
    title: "Command send failed",
    explanation: "The active transport rejected or failed the write request.",
    fieldLabels: { mode: "Transport mode (mode)", error: "Error detail (error)" },
    severity: "error"
  },
  BLE_RX50: {
    category: "BLE",
    title: "BLE notify receive statistics",
    explanation: "Periodic BLE packet, byte, failure, and prefix counters.",
    fieldLabels: {
      packets: "Notify packet count (packets)",
      bytes: "Notify byte count (bytes)",
      reassembled: "Reassembled payload count (reassembled)",
      fail: "Failure count (fail)",
      prefix: "Last packet prefix (prefix)",
      ...commonFields
    },
    severity: countersSeverity(["fail", "missing", "timeout", "crc", "length"])
  },
  BLE_FRAG50: {
    category: "BLE",
    title: "BLE fragment statistics",
    explanation: "BLE fragment reassembly counters including duplicate, missing, timeout, CRC, and length failures.",
    fieldLabels: {
      rx: "Fragment receive count (rx)",
      reassembled: "Reassembled fragment count (reassembled)",
      duplicate: "Duplicate fragment count (duplicate)",
      missing: "Missing fragment count (missing)",
      timeout: "Fragment timeout count (timeout)",
      crc: "CRC failure count (crc)",
      length: "Length failure count (length)",
      ...commonFields
    },
    severity: countersSeverity(["missing", "timeout", "crc", "length"])
  },
  PROTO50: {
    category: "Parser",
    title: "Protocol parser statistics",
    explanation: "Content router counters for measurement frames, text logs, rejects, and accepted frames.",
    fieldLabels: {
      src: "Envelope source (src)",
      ch: "Envelope channel (ch)",
      cap: "Capacitance frame count (cap)",
      volt: "Voltage frame count (volt)",
      res: "Resistance frame count (res)",
      log: "Log line count (log)",
      reject: "Parser reject count (reject)",
      frame: "Accepted frame count (frame)",
      ...commonFields
    },
    severity: countersSeverity(["reject", "err", "fail"])
  }
};

function batterySchema(title: string): LogTagSchema {
  return {
    category: "Battery",
    title,
    explanation: "Battery, ADS, and analog rail telemetry parsed from firmware logs.",
    fieldLabels: {
      read: "Read result (read)",
      state: "Battery state (state)",
      bt: "Battery millivolts (bt)",
      br: "Battery reading reason (br)",
      bs: "Battery state flag (bs)",
      a8d: "ADS AIN8 differential raw (a8d)",
      ac: "ADS current raw (ac)",
      a8g: "ADS AIN8 gain raw (a8g)",
      rail: "Analog rail microvolts (rail)",
      rv: "Rail voltage valid (rv)",
      rs: "Rail status (rs)",
      re: "Rail error microvolts (re)",
      age: "Sample age (age)",
      z: "Zero offset pair (z)",
      fresh: "Fresh sample flag (fresh)",
      valid: "Valid sample flag (valid)",
      ageMs: "Sample age milliseconds (ageMs)",
      reason: "Battery reason (reason)",
      restore: "Restore result (restore)",
      retry: "Recovered retry count (retry)",
      unstable: "Unstable sample count/flag (unstable)",
      timeout: "Timeout count/flag (timeout)",
      spreadRaw: "Raw sample spread (spreadRaw)",
      spreadMaxRaw: "Maximum allowed raw spread (spreadMaxRaw)",
      validRun: "Consecutive valid samples (validRun)",
      invalidRun: "Consecutive invalid samples (invalidRun)",
      status: "ADS status byte (status)",
      dg: "Diagnostic flag (dg)",
      chip: "ADS chip model (chip)"
    },
    severity: (_row, details) => (details.bt === -1 || details.br === "range_error" || details.bs === "stale" ? "warn" : "ok")
  };
}

function diagnosticSeverity(row: LogRow, details: Record<string, LogFieldValue>): StatusSeverity {
  if (row.severity === "error" || details.state === "failed" || details.state === "error" || Boolean(details.error)) {
    return "error";
  }
  if (details.restore === false || details.restore === "failed") {
    return "warn";
  }
  return details.state === "completed" || details.state === "done" ? "ok" : "info";
}

function countersSeverity(keys: string[]): (row: LogRow, details: Record<string, LogFieldValue>) => StatusSeverity {
  return (row, details) => {
    if (row.severity === "error") {
      return "error";
    }
    return keys.some((key) => counterHasProblem(details[key])) ? "warn" : "ok";
  };
}

function counterHasProblem(value: LogFieldValue): boolean {
  if (typeof value === "number") {
    return value > 0;
  }
  if (typeof value === "string") {
    return value.split("/").some((part) => Number(part) > 0);
  }
  return false;
}
