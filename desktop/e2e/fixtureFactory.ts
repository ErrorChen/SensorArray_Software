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
  malformedRecovery: string;
  rows: Record<1 | 2 | 4 | 8, string>;
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
      voltageSource
    ].join("")
  );
  const modeTimeline = writeReplay(
    outputRoot,
    "mode_pending_applied.replay",
    [
      cap8Source,
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
  const capReturn = writeReplay(
    outputRoot,
    "cap_return.replay",
    [
      "MACK,id=43,old=CAP,new=RES,state=accepted\n",
      "MAPP,id=43,gen=8,old=CAP,new=RES,seq=9,state=applied,transitionUs=600\n",
      resistanceSource,
      "MACK,id=44,old=RES,new=CAP,state=accepted\n",
      "MAPP,id=44,gen=9,old=RES,new=CAP,seq=1,state=applied,transitionUs=400\n",
      cap8Source
    ].join("")
  );

  const diagnosticSource = readFixture(path.join(sourceRoot, "current_protocol", "diagnostics.txt"));
  const diagnosticLines = diagnosticSource
    .split(/(?<=\n)/)
    .filter((line) => /^(ADS,|ACK,cmd=ADSCHK|ADSCHK,|ADSCHKSTAT,|ABAT,|BAPP,)/.test(line))
    .join("");
  const diagnostics = writeReplay(outputRoot, "diagnostics.replay", diagnosticLines);

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

  const rows = {
    1: path.join(sourceRoot, "b41", "rows1_valid.txt"),
    2: path.join(sourceRoot, "b41", "rows2_valid.txt"),
    4: path.join(sourceRoot, "b41", "rows4_valid.txt"),
    8: path.join(sourceRoot, "b41", "rows8_valid.txt")
  } satisfies Record<1 | 2 | 4 | 8, string>;

  return { cap8, modeTimeline, voltage, oldGeneration, crcRecovery, resistance, capReturn, diagnostics, malformedRecovery, rows };
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
