import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

export type GuiReplayFixtures = {
  cap8: string;
  modeTimeline: string;
  voltage: string;
  oldGeneration: string;
  crcRecovery: string;
  resistance: string;
  capReturn: string;
  diagnostics: string;
  batteryStale: string;
  malformedRecovery: string;
  rows1Res: string;
  mixed5: string;
  mixed8: string;
  rows: Record<1 | 2 | 3 | 4 | 5 | 6 | 7 | 8, string>;
};

/**
 * Build GUI acceptance streams from checked-in firmware-reference packets.
 *
 * The source V/R packets are copied from tests/fixtures/current_protocol and
 * their CRC is independently checked before any derived frame is emitted.
 * Derived packets only change explicit header/value fields and recalculate the
 * same reflected CRC32 over header + D + P lines (each including LF).
 */
export function prepareGuiReplayFixtures(repoRoot: string): GuiReplayFixtures {
  const sourceRoot = path.join(repoRoot, "tests", "fixtures");
  const outputRoot = path.join(repoRoot, "validation_artifacts", "replay");
  mkdirSync(outputRoot, { recursive: true });

  const cap8Source = readFixture(path.join(sourceRoot, "b41", "rows8_valid.txt"));
  const voltageSource = readFixture(path.join(sourceRoot, "current_protocol", "volt_rows2_mixed.txt"));
  const resistanceSource = readFixture(path.join(sourceRoot, "current_protocol", "res_rows1_mixed.txt"));
  verifyPacketCrc(cap8Source);
  verifyPacketCrc(voltageSource);
  verifyPacketCrc(resistanceSource);

  const capInvalid = mutatePacket(cap8Source, {
    headerFields: { seq: "9", ts: "9000", bad: "0/0/1" },
    firstDataToken: "-1000000"
  });
  const cap8 = writeReplay(outputRoot, "cap_8x8.replay", `${cap8Source}${capInvalid}`);

  const voltage = writeReplay(
    outputRoot,
    "voltage.replay",
    [
      "MACK,id=42,old=CAP,new=VOLT,state=accepted\n",
      "MAPP,id=42,gen=7,old=CAP,new=VOLT,seq=8,state=applied,transitionUs=500\n",
      voltageSource,
      // Exact RAIL? response keys from firmware 331c445. `src=monitor` is
      // normalised by the typed backend to the UI-facing internal_monitor.
      "ARL,src=monitor,raw=123,mon=5126000,rail=5126000,avdd=3126000,avss=-2000000,exp=5126000,err=0,rv=1,rs=ok,age=0,ref=restored,pwr=restored,mux=restored\n",
      // Keep the synthetic device connected while the Electron assertion and
      // screenshot run. Reaching Replay EOF deliberately marks telemetry
      // connection-stale, which is tested separately and must not be confused
      // with the fresh ARL production record above.
      "@delay-ms=60000\n"
    ].join("")
  );
  const modeTimeline = writeReplay(
    outputRoot,
    "mode_pending_applied.replay",
    [
      // Keep geometry stable across the mode transaction.  The reference
      // VOLT packet has ROWS=2; jumping here from an authoritative ROWS=8 CAP
      // frame without RCMD/RAPP would correctly be rejected by MatrixStore's
      // independent geometry gate and would test malformed Replay data rather
      // than MACK -> MAPP semantics.
      buildCapPacket(cap8Source, 2),
      "MACK,id=42,old=CAP,new=VOLT,state=accepted\n",
      "@delay-ms=2500\n",
      "MAPP,id=42,gen=7,old=CAP,new=VOLT,seq=8,state=applied,transitionUs=500\n",
      voltageSource
    ].join("")
  );

  const oldGenerationFrame = mutatePacket(voltageSource, {
    headerFields: { seq: "9", ts: "124000", gen: "6" },
    firstDataToken: "-999999"
  });
  const currentGenerationFrame = mutatePacket(voltageSource, {
    headerFields: { seq: "10", ts: "125000", gen: "7" },
    firstDataToken: "-2500"
  });
  const oldGeneration = writeReplay(
    outputRoot,
    "old_generation.replay",
    [
      "MACK,id=42,old=CAP,new=VOLT,state=accepted\n",
      "MAPP,id=42,gen=7,old=CAP,new=VOLT,seq=8,state=applied,transitionUs=500\n",
      voltageSource,
      oldGenerationFrame,
      "@delay-ms=2000\n",
      currentGenerationFrame
    ].join("")
  );

  const crcBadFrame = mutatePacket(voltageSource, {
    headerFields: { seq: "9", ts: "124000", gen: "7" },
    firstDataToken: "-888888"
  }).replace(/crc=[0-9A-F]{8}/, "crc=00000000");
  const crcRecovery = writeReplay(
    outputRoot,
    "crc_recovery.replay",
    [
      "MACK,id=42,old=CAP,new=VOLT,state=accepted\n",
      "MAPP,id=42,gen=7,old=CAP,new=VOLT,seq=8,state=applied,transitionUs=500\n",
      voltageSource,
      crcBadFrame,
      "@delay-ms=2000\n",
      currentGenerationFrame
    ].join("")
  );

  const resistance = writeReplay(
    outputRoot,
    "resistance.replay",
    [
      "MACK,id=43,old=CAP,new=RES,state=accepted\n",
      "MAPP,id=43,gen=8,old=CAP,new=RES,seq=9,state=applied,transitionUs=600\n",
      resistanceSource
    ].join("")
  );
  const rows1ResPacket = buildSingleValueResistancePacket(resistanceSource, 10_025_000);
  const rows1Res = writeReplay(
    outputRoot,
    "rows1_res_10025.replay",
    [
      "MACK,id=91,old=CAP,new=RES,state=accepted\n",
      "MAPP,id=91,gen=18,old=CAP,new=RES,seq=91,state=applied,transitionUs=600\n",
      rows1ResPacket
    ].join("")
  );
  const capReturn = writeReplay(
    outputRoot,
    "cap_return.replay",
    [
      "MACK,id=43,old=CAP,new=RES,state=accepted\n",
      "MAPP,id=43,gen=8,old=CAP,new=RES,seq=9,state=applied,transitionUs=600\n",
      resistanceSource,
      "MACK,id=44,old=RES,new=CAP,state=accepted\n",
      "MAPP,id=44,gen=9,old=RES,new=CAP,seq=1,state=applied,transitionUs=400\n",
      // MODE changes the quantity for all saved rows; it does not change the
      // independent ROWS geometry.  Keep the active ROWS=1 established by
      // the resistance frame instead of fabricating an implicit 1 -> 8 jump.
      buildCapPacket(cap8Source, 1)
    ].join("")
  );

  const diagnosticSource = readFixture(path.join(sourceRoot, "current_protocol", "diagnostics.txt"));
  const diagnosticLines = diagnosticSource
    .split(/(?<=\n)/)
    .filter((line) => /^(ADS,|ACK,cmd=ADSCHK|ADSCHK,|ADSCHKSTAT,|ABAT,|BAPP,)/.test(line))
    .join("");
  const diagnostics = writeReplay(outputRoot, "diagnostics.replay", diagnosticLines);
  const batteryStale = writeReplay(
    outputRoot,
    "battery_stale.replay",
    [
      "ABAT,bt=4092,valid=1,fresh=1,ageMs=0,lastGoodMv=4092,lastGoodValid=1,lastGoodFresh=1,lastGoodAgeMs=0,lastGoodFrame=91,periodMs=1000,reason=ok\n",
      "@delay-ms=500\n",
      "ABAT,bt=-1,valid=0,fresh=0,ageMs=0,lastGoodMv=4092,lastGoodValid=1,lastGoodFresh=1,lastGoodAgeMs=500,lastGoodFrame=91,periodMs=1000,reason=adc_timeout\n"
    ].join("")
  );

  const missingP = voltageSource
    .split(/(?<=\n)/)
    .filter((line) => !line.startsWith("P0,"))
    .join("");
  const duplicateDLines = voltageSource.split(/(?<=\n)/);
  const duplicateDIndex = duplicateDLines.findIndex((line) => line.startsWith("D0,"));
  duplicateDLines.splice(duplicateDIndex + 1, 0, duplicateDLines[duplicateDIndex]);
  const badX = voltageSource.replace("D0,-1250", "D0,XG1");
  const malformedRecovery = writeReplay(
    outputRoot,
    "malformed_recovery.replay",
    [
      missingP,
      duplicateDLines.join(""),
      badX,
      crcBadFrame,
      "FUTURE99,alpha=1,message=forward-compatible\n",
      cap8Source
    ].join("")
  );

  const rows = Object.fromEntries(
    Array.from({ length: 8 }, (_, index) => {
      const rowCount = index + 1;
      const packet = buildCapPacket(cap8Source, rowCount);
      return [rowCount, writeReplay(outputRoot, `cap_rows${rowCount}.replay`, packet)];
    })
  ) as Record<1 | 2 | 3 | 4 | 5 | 6 | 7 | 8, string>;

  const mixed5 = writeReplay(
    outputRoot,
    "mixed_rows5_crvcrvcr.replay",
    [
      "RMACK,id=63,old=CCCCCCCC,new=CRVCRVCR,state=accepted\n",
      "RMAPP,id=63,gen=12,seq=201,profile=CRVCRVCR,state=applied\n",
      buildMixedPacket({ rows: 5, seq: 201, profile: "CRVCRVCR", profileGeneration: 12, profileRequestId: 63 })
    ].join("")
  );
  const mixed8 = writeReplay(
    outputRoot,
    "mixed_rows8_rvvccvvr.replay",
    [
      "RMACK,id=62,old=CCCCCCCC,new=RVVCCVVR,state=accepted\n",
      "RMAPP,id=62,gen=11,seq=202,profile=RVVCCVVR,state=applied\n",
      buildMixedPacket({ rows: 8, seq: 202, profile: "RVVCCVVR", profileGeneration: 11, profileRequestId: 62 })
    ].join("")
  );
  return {
    cap8,
    modeTimeline,
    voltage,
    oldGeneration,
    crcRecovery,
    resistance,
    rows1Res,
    mixed5,
    mixed8,
    capReturn,
    diagnostics,
    batteryStale,
    malformedRecovery,
    rows
  };
}

type PacketMutation = {
  headerFields?: Record<string, string>;
  firstDataToken?: string;
};

function mutatePacket(packet: string, mutation: PacketMutation): string {
  const lines = packet.trimEnd().split("\n");
  let header = lines[0];
  for (const [field, value] of Object.entries(mutation.headerFields ?? {})) {
    const expression = new RegExp(`(^|,)${escapeExpression(field)}=[^,]+`);
    if (!expression.test(header)) {
      throw new Error(`fixture header does not contain ${field}: ${header}`);
    }
    header = header.replace(expression, (_match, prefix: string) => `${prefix}${field}=${value}`);
  }
  lines[0] = header;
  if (mutation.firstDataToken !== undefined) {
    const dataIndex = lines.findIndex((line) => line.startsWith("D0,"));
    if (dataIndex < 0) {
      throw new Error("fixture packet has no D0 line");
    }
    const parts = lines[dataIndex].split(",");
    parts[1] = mutation.firstDataToken;
    lines[dataIndex] = parts.join(",");
  }
  return packetWithCrc(lines.join("\n"));
}

function buildCapPacket(sourcePacket: string, rows: number): string {
  if (!Number.isInteger(rows) || rows < 1 || rows > 8) {
    throw new Error(`CAP rows must be 1..8, received ${rows}`);
  }
  const sourceLines = sourcePacket.trimEnd().split("\n");
  const values = sourceLines
    .filter((line) => /^D\d+,/.test(line))
    .flatMap((line) => line.split(",").slice(1))
    .slice(0, rows * 8);
  const activeMask = ((1 << rows) - 1).toString(16).toUpperCase().padStart(2, "0");
  let header = sourceLines[0];
  const fields: Record<string, string> = {
    seq: String(rows),
    ts: String(rows * 1000),
    rows: String(rows),
    cells: String(rows * 8),
    rf: activeMask,
    pf: activeMask,
    sf: activeMask,
    n: String(rows * 8)
  };
  for (const [field, value] of Object.entries(fields)) {
    header = header.replace(new RegExp(`(^|,)${field}=[^,]+`), (_match, prefix: string) => `${prefix}${field}=${value}`);
  }
  const lines = [header];
  for (let index = 0; index < values.length; index += 16) {
    lines.push(`D${Math.floor(index / 16)},${values.slice(index, index + 16).join(",")}`);
  }
  lines.push("K,seq=0,gen=0,rid=0,crc=00000000");
  return packetWithCrc(lines.join("\n"));
}

function buildSingleValueResistancePacket(sourcePacket: string, rawMilliOhms: number): string {
  const lines = sourcePacket.trimEnd().split("\n");
  let header = lines[0];
  const fields: Record<string, string> = {
    seq: "91",
    ts: "91000",
    gen: "18",
    rid: "91",
    valid: "0000000000000001",
    fresh: "0000000000000001",
    error: "00000000000000FE",
    bad: "7"
  };
  for (const [field, value] of Object.entries(fields)) {
    header = header.replace(new RegExp(`(^|,)${field}=[^,]+`), (_match, prefix: string) => `${prefix}${field}=${value}`);
  }
  lines[0] = header;
  const dataIndex = lines.findIndex((line) => line.startsWith("D0,"));
  lines[dataIndex] = `D0,${rawMilliOhms},X01,X01,X01,X01,X01,X01,X01`;
  return packetWithCrc(lines.join("\n"));
}

export function buildMixedPacket(options: {
  rows: number;
  seq: number;
  profile: string;
  profileGeneration: number;
  profileRequestId: number;
}): string {
  const { rows, seq, profile, profileGeneration, profileRequestId } = options;
  if (!/^[CVR]{8}$/.test(profile) || rows < 1 || rows > 8) {
    throw new Error(`invalid mixed fixture geometry/profile: rows=${rows}, profile=${profile}`);
  }
  const modes = {
    C: { unit: "pF", scale: -6, format: "pf6" },
    V: { unit: "V", scale: -6, format: "uv-x" },
    R: { unit: "ohm", scale: -3, format: "mohm-x" }
  } as const;
  const rowsGeneration = 4;
  const rowsRequestId = 14;
  const lines = [
    `M,seq=${seq},ts=${seq * 1000},rows=${rows},cells=${rows * 8},rgen=${rowsGeneration},rrid=${rowsRequestId},pgen=${profileGeneration},prid=${profileRequestId},profile=${profile},fmt=mix1`
  ];
  for (let row = 1; row <= rows; row += 1) {
    const mode = profile[row - 1] as keyof typeof modes;
    const descriptor = modes[mode];
    const values = Array.from({ length: 8 }, (_, cell) => {
      if (mode === "C") return String(39_000_000 + row * 100_000 + cell * 1_000);
      if (mode === "V") return String(-1_000_000 + row * 100_000 + cell * 10_000);
      return String(10_025_000 + row * 1_000 + cell * 10);
    });
    lines.push(
      `MR,s=${row},m=${mode},unit=${descriptor.unit},scale=${descriptor.scale},valid=FF,fresh=FF,error=00,fmt=${descriptor.format},D=${values.join(",")}`
    );
  }
  const payload = `${lines.join("\n")}\n`;
  const crc = crc32(Buffer.from(payload, "ascii")).toString(16).toUpperCase().padStart(8, "0");
  return `${payload}K,seq=${seq},rgen=${rowsGeneration},rrid=${rowsRequestId},pgen=${profileGeneration},prid=${profileRequestId},crc=${crc}\n`;
}

function packetWithCrc(packet: string): string {
  const lines = packet.trimEnd().split("\n");
  const trailerIndex = lines.findIndex((line) => line.startsWith("K,"));
  if (trailerIndex < 0) {
    throw new Error("fixture packet has no K trailer");
  }
  const header = lines[0];
  const seq = requiredField(header, "seq");
  const generation = requiredField(header, "gen");
  const requestId = requiredField(header, "rid");
  const payload = `${lines.slice(0, trailerIndex).join("\n")}\n`;
  const crc = crc32(Buffer.from(payload, "ascii")).toString(16).toUpperCase().padStart(8, "0");
  return `${payload}K,seq=${seq},gen=${generation},rid=${requestId},crc=${crc}\n`;
}

function verifyPacketCrc(packet: string): void {
  const lines = packet.trimEnd().split("\n");
  const trailerIndex = lines.findIndex((line) => line.startsWith("K,"));
  const expected = requiredField(lines[trailerIndex], "crc").toUpperCase();
  const payload = `${lines.slice(0, trailerIndex).join("\n")}\n`;
  const actual = crc32(Buffer.from(payload, "ascii")).toString(16).toUpperCase().padStart(8, "0");
  if (actual !== expected) {
    throw new Error(`firmware reference fixture CRC mismatch: expected ${expected}, got ${actual}`);
  }
}

function crc32(payload: Uint8Array): number {
  let crc = 0xffffffff;
  for (const byte of payload) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
    }
  }
  return (~crc) >>> 0;
}

function requiredField(line: string, field: string): string {
  const match = new RegExp(`(?:^|,)${escapeExpression(field)}=([^,]+)`).exec(line);
  if (!match) {
    throw new Error(`fixture line does not contain ${field}: ${line}`);
  }
  return match[1];
}

function readFixture(filePath: string): string {
  const value = readFileSync(filePath, "ascii").replace(/\r\n/g, "\n");
  return value.endsWith("\n") ? value : `${value}\n`;
}

function writeReplay(outputRoot: string, name: string, value: string): string {
  const filePath = path.join(outputRoot, name);
  writeFileSync(filePath, value.replace(/\r\n/g, "\n"), "ascii");
  return filePath;
}

function escapeExpression(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
