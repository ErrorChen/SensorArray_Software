import { describe, expect, it } from "vitest";

import { buildCapPacket, buildMixedPacket } from "./fixtureFactory";

describe("firmware-current Replay fixture grammar", () => {
  it("limits CAP acquisition masks to active cells for ROWS 1..8", () => {
    const source = [
      "C,seq=8,ts=8000,rows=8,cells=64,gen=12,rid=9,rf=FF,pf=FF,sf=FF,expected=FFFFFFFFFFFFFFFF,acquired=FFFFFFFFFFFFFFFF,bad=0/0/0,fmt=pf6,n=64",
      ...Array.from({ length: 4 }, (_, line) => `D${line},${Array.from({ length: 16 }, () => "33000000").join(",")}`),
      "K,seq=8,gen=12,rid=9,crc=00000000"
    ].join("\n");

    expect(buildCapPacket(source, 1).split("\n", 1)[0]).toContain("expected=00000000000000FF,acquired=00000000000000FF");
    expect(buildCapPacket(source, 5).split("\n", 1)[0]).toContain("expected=000000FFFFFFFFFF,acquired=000000FFFFFFFFFF");
    expect(buildCapPacket(source, 8).split("\n", 1)[0]).toContain("expected=FFFFFFFFFFFFFFFF,acquired=FFFFFFFFFFFFFFFF");
  });

  it("matches the 8045e9e9 M/MR/K formatter and CRC contract", () => {
    const packet = buildMixedPacket({
      rows: 5,
      seq: 201,
      profile: "CRVCRVCR",
      profileGeneration: 12,
      profileRequestId: 63
    });
    const lines = packet.trimEnd().split("\n");

    expect(lines[0]).toBe(
      "M,seq=201,ts=201000,rows=5,cells=40,rgen=4,rrid=14,pgen=12,prid=63,profile=CRVCRNNN,expected=000000FFFFFFFFFF,acquired=000000FFFFFFFFFF,fmt=mix1"
    );
    expect(lines.slice(1, -1)).toHaveLength(5);
    expect(lines[1]).toMatch(
      /^MR,s=1,m=CAP,unit=pF,scale=-6,expected=FF,acquired=FF,valid=FF,fresh=FF,error=00,fmt=pf6,D=(?:-?\d+,){7}-?\d+$/
    );
    expect(lines[2]).toMatch(
      /^MR,s=2,m=RES,unit=ohm,scale=-3,expected=FF,acquired=FF,valid=FF,fresh=FF,error=00,fmt=mohm-x,D=(?:-?\d+,){7}-?\d+$/
    );
    expect(lines[3]).toMatch(
      /^MR,s=3,m=VOLT,unit=V,scale=-6,expected=FF,acquired=FF,valid=FF,fresh=FF,error=00,fmt=uv-x,D=(?:-?\d+,){7}-?\d+$/
    );
    expect(packet).not.toMatch(/rowsGen=|profileGen=|\brow=|\bmode=|values=|pga=/);

    const trailer = /^K,seq=201,rgen=4,rrid=14,pgen=12,prid=63,crc=([0-9A-F]{8})$/.exec(lines.at(-1) ?? "");
    expect(trailer).not.toBeNull();
    const crcPayload = Buffer.from(`${lines.slice(0, -1).join("\n")}\n`, "ascii");
    expect(trailer?.[1]).toBe(crc32(crcPayload).toString(16).toUpperCase().padStart(8, "0"));
  });
});

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
