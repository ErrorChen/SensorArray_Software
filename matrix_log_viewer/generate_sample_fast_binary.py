from __future__ import annotations

import binascii
import struct
from pathlib import Path

from matrix_log_viewer.binary_frame_parser import FMT, FRAME_TYPE_VOLTAGE_COMPACT, MAGIC, SIZE

ALL_VALID = (1 << 64) - 1


def build_frame(
    seq: int,
    timestamp_us: int | None = None,
    *,
    valid_mask: int = ALL_VALID,
    crc_delta: int = 0,
    dropped: int = 0,
    decimated: int = 0,
    output_divider: int = 1,
    values: list[int] | None = None,
) -> bytes:
    timestamp = timestamp_us if timestamp_us is not None else seq * 33_333
    cell_values = values if values is not None else [seq * 100 + index for index in range(64)]
    fields = [
        MAGIC,
        1,
        FRAME_TYPE_VOLTAGE_COMPACT,
        seq,
        timestamp,
        31_000,
        0,
        0,
        0,
        min(0xFFFF, int(dropped)),
        min(0xFFFF, int(decimated)),
        valid_mask,
        *cell_values,
        15,
        min(255, int(output_divider)),
        0,
        0,
    ]
    raw = struct.pack(FMT, *fields)
    fields[-1] = (binascii.crc32(raw[: SIZE - 4]) + crc_delta) & 0xFFFFFFFF
    return struct.pack(FMT, *fields)


def startup_lines(*, partial_after_first_byte: int = 0, drop: int = 0, decimated: int = 0, output_div: int = 1) -> bytes:
    return b"".join(
        [
            b"RESET_REASON,reason=1,heapFree=240000,heapMinFree=220000\n",
            b"APPMODE,active=FAST_BINARY_FAKE,cnName=FastBinaryFake,skipAdsInit=1,skipFdcInit=1,sw=DEBUG\n",
            b"BUILD_CONFIG,profile=FAST,output=binary,dedicatedTasks=1,queueDepth=16,usbStdoutNonblocking=1,autoRateControl=1,binaryPureMode=1,binaryAllowStartupText=1,fastBinaryStartupDiagMs=5000,usbExactBinaryWrite=1,framePoolSize=16,dropPolicy=oldest,fakeFps=100\n",
            b"VOLTSCAN_CONFIG,mode=FAST_BINARY_FAKE,dr=15,format=binary,queueDepth=16,csvEvery=0,autoRate=1,usbNonblocking=1\n",
            b"STREAM_MEM,streamFrameSize=328,compactFrameSize=312,queueDepth=16,queueBytes=128,poolSize=16,hotPath=compact,commStack=16384,scanStack=12288,heapFree=240000,heapMinFree=220000\n",
            (
                "FAST_BINARY_DIAG,seq=0,scanFps=100,outFps=100,scanAvgUs=9000,routeAvgUs=500,"
                "inpmuxAvgUs=8,drdyAvgUs=24,adcReadAvgUs=14,spiAvgUs=5,usbAvgUs=90,"
                f"qUsed=0,qFull=0,drop={drop},shortWrite=0,writeFail=0,outputDiv={output_div},"
                f"droppedBeforeFirstByte=0,partialAfterFirstByte={partial_after_first_byte},"
                "fullFrameWriteCount=0,fullFrameWriteFailCount=0,heapFree=240000,heapMinFree=220000,"
                "outStackMinWords=4000,scanStackMinWords=5000\n"
            ).encode("ascii"),
            b"FAST_BINARY_START,magic=0x31434153,magicBytes=SAC1,version=1,frameType=0x1261,frameSize=312,pure=1\n",
        ]
    )


def write_samples() -> list[Path]:
    output_dir = Path(__file__).resolve().parent / "sample_logs"
    output_dir.mkdir(parents=True, exist_ok=True)
    samples: dict[str, bytes] = {}

    samples["sample_upper_speed_startup_then_binary.bin"] = startup_lines() + b"".join(build_frame(seq) for seq in range(1, 21))
    samples["sample_upper_speed_pure_binary.bin"] = b"".join(build_frame(seq) for seq in range(1, 21))

    bad = build_frame(2, crc_delta=1)
    samples["sample_upper_speed_crc_resync.bin"] = build_frame(1) + bad + b"garbage\x00\x81" + build_frame(3)

    samples["sample_upper_speed_ascii_after_start.bin"] = (
        startup_lines()
        + build_frame(1)
        + b"STAT,seq=1,scanFps=100,outFps=100,drop=0,decimated=0,code=0x0000\n"
        + b"MATV,1,1000,100,uV,1,2,3\n"
        + build_frame(2)
    )

    invalid_s4d4 = ALL_VALID & ~(1 << ((4 - 1) * 8 + (4 - 1)))
    samples["sample_upper_speed_validmask.bin"] = startup_lines() + build_frame(1) + build_frame(2, valid_mask=invalid_s4d4)

    samples["sample_upper_speed_device_drop_decimate.bin"] = (
        startup_lines(drop=3, decimated=5, output_div=4)
        + build_frame(1, dropped=0, decimated=0, output_divider=1)
        + build_frame(2, dropped=3, decimated=5, output_divider=4)
    )
    samples["sample_upper_speed_partial_risk.bin"] = startup_lines(partial_after_first_byte=1) + build_frame(1)

    written = []
    for name, payload in samples.items():
        path = output_dir / name
        path.write_bytes(payload)
        written.append(path)
    legacy = output_dir / "sample_fast_binary_mixed.bin"
    legacy.write_bytes(samples["sample_upper_speed_startup_then_binary.bin"])
    written.append(legacy)
    return written


def main() -> int:
    for path in write_samples():
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
