from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from sensorarray_app.constants import CAP_INVALID_SENTINEL  # noqa: E402
from sensorarray_app.protocol.crc import crc32_reflected  # noqa: E402


def build_frame(seq: int, rows: int, gen: int = 12, rid: int = 9, start: int = 33_000_000) -> str:
    cells = rows * 8
    values = [start + index * 1_000_000 for index in range(cells)]
    active_mask = (1 << cells) - 1 if cells < 64 else (1 << 64) - 1
    header = (
        f"C,seq={seq},ts={seq * 1000},rows={rows},cells={cells},gen={gen},rid={rid},"
        f"rf={(1 << rows) - 1:02X},pf={(1 << rows) - 1:02X},sf={(1 << rows) - 1:02X},"
        f"expected={active_mask:016X},acquired={active_mask:016X},bad=0/0/0,fmt=pf6,n={cells}\n"
    )
    lines = [header]
    for idx in range((cells + 15) // 16):
        chunk = values[idx * 16 : (idx + 1) * 16]
        lines.append(f"D{idx}," + ",".join(str(value) for value in chunk) + "\n")
    crc = crc32_reflected("".join(lines).encode("ascii"))
    lines.append(f"K,seq={seq},gen={gen},rid={rid},crc={crc:08X}\n")
    return "".join(lines)


def write(path: Path, text: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text, encoding="ascii", newline="\n")


def main() -> None:
    b41 = ROOT / "tests" / "fixtures" / "b41"
    write(b41 / "rows1_valid.txt", build_frame(1, 1))
    write(b41 / "rows2_valid.txt", build_frame(2, 2))
    write(b41 / "rows4_valid.txt", build_frame(4, 4))
    write(b41 / "rows8_valid.txt", build_frame(8, 8))
    invalid = (
        build_frame(10, 1)
        .replace("33000000", str(CAP_INVALID_SENTINEL), 1)
        .replace("bad=0/0/0", "bad=0/0/1", 1)
    )
    # Regenerate CRC after changing the payload.
    body = "\n".join(invalid.splitlines()[:-1]) + "\n"
    trailer = f"K,seq=10,gen=12,rid=9,crc={crc32_reflected(body.encode('ascii')):08X}\n"
    write(b41 / "invalid_sentinel.txt", body + trailer)
    write(b41 / "crc_failure.txt", build_frame(11, 2).replace("crc=", "crc=DEADBEEF", 1))
    lines = build_frame(12, 2).splitlines(keepends=True)
    write(b41 / "missing_data.txt", "".join([lines[0], *lines[2:]]))
    duplicate = build_frame(13, 2).splitlines(keepends=True)
    write(b41 / "duplicate_data.txt", "".join([duplicate[0], duplicate[1], duplicate[1], *duplicate[2:]]))
    write(b41 / "sequence_gap.txt", build_frame(20, 1) + build_frame(22, 1))
    write(b41 / "generation_change.txt", build_frame(30, 1, gen=1) + build_frame(31, 1, gen=2))
    write(b41 / "rcmd_rapp.txt", "RCMD,id=7,old=8,req=4,status=accepted,generation=3\nRAPP,id=7,seq=101,old=8,new=4,gen=4,status=applied\n")
    write(b41 / "sf50.txt", "SF50,seq=151-200,n=50,rows=8,cfps=11.11,efps=11.11,ofps=0.9/0.0/0.0,bad=0/0/0,drop=0/0/0,q=1/2\n")
    write(b41 / "tr50.txt", "TR50,r=8,fu=90000,rau=10278,rmu=11157,rt=51,wt=4366,rp=2622,rs=2622,co=9892,ag=8,agn=0,ags=0,agf=1\n")
    write(b41 / "ab50_valid.txt", "AB50,bt=3870,br=unknown,bs=present,a8d=100,ac=200,a8g=300,rail=5171184,rv=1,rs=ok,re=71184,age=1,z=-48/0,fresh=1,status=0x61,dg=1,chip=1262\n")
    write(b41 / "ab50_invalid.txt", "AB50,bt=-1,br=range_error,bs=stale,a8d=1812236,ac=760468,a8g=2572704,rail=5171184,rv=1,rs=ok,re=71184,age=200,z=-48/0,fresh=1,status=0x61,dg=1,chip=1262\n")
    write(b41 / "batd.txt", "BATD,read=ok,state=present,bt=3870,br=unknown,fresh=1,status=0x61,dg=1,a8d=100,ac=200,a8g=300,z=-1/2\n")
    write(b41 / "arl.txt", "ARL,rail=5171184,rv=1,rs=ok,re=71184\n")
    write(b41 / "ads.txt", "ADS,chip=1262,dev=0,rev=3,adc=ADC1\n")
    write(b41 / "serial_mixed_stream.bin", ("RST,reason=poweron\n" + build_frame(40, 4) + "AB50,bt=-1,br=range_error,bs=stale\n").encode("ascii"))
    write(b41 / "ble_single_packet.json", json.dumps({"channel": "data", "payload": build_frame(50, 1)}, indent=2))
    message = build_frame(51, 2).encode("ascii")
    crc = crc32_reflected(message)
    half = len(message) // 2
    fragments = [
        f"G,data,9,0,2,{half},{len(message)},{crc:08X}\n".encode("ascii") + message[:half],
        f"G,data,9,1,2,{len(message) - half},{len(message)},{crc:08X}\n".encode("ascii") + message[half:],
    ]
    write(b41 / "ble_fragmented_data.json", json.dumps([fragment.decode("ascii") for fragment in fragments], indent=2))
    log_message = b"AB50,bt=3870,br=unknown,bs=present\n"
    log_crc = crc32_reflected(log_message)
    write(
        b41 / "ble_fragmented_log.json",
        json.dumps([f"G,log,10,0,1,{len(log_message)},{len(log_message)},{log_crc:08X}\n".encode("ascii").decode("ascii") + log_message.decode("ascii")], indent=2),
    )
    write(b41 / "wifi_data_datagrams.json", json.dumps([build_frame(60, 1)], indent=2))
    write(b41 / "wifi_log_datagrams.json", json.dumps(["AB50,bt=3870,br=unknown,bs=present\n"], indent=2))
    write(b41 / "wifi_ctrl_datagrams.json", json.dumps(["ACK,cmd=BAT?\n"], indent=2))
    (ROOT / "tests" / "fixtures" / "legacy_binary").mkdir(parents=True, exist_ok=True)
    (ROOT / "tests" / "fixtures" / "legacy_matv").mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    main()
