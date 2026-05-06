from __future__ import annotations

from typing import Optional

from .protocol_types import MatrixFrame
from .text_log_parser import TextLogParser

MatvFrame = MatrixFrame


class MatvParser:
    """Compatibility wrapper around the new CSV text parser."""

    def __init__(self):
        self._parser = TextLogParser()

    def parseLine(self, line: str) -> Optional[MatvFrame]:
        result = self._parser.parseLine(line)
        if result is None or result.frame is None or result.frame.frameType != "MATV":
            return None
        return result.frame

    def getStats(self) -> dict:
        stats = self._parser.getStats()
        return {
            "matvFramesParsed": stats.get("parsedByType", {}).get("MATV", 0),
            "skippedLines": stats.get("skippedLines", 0),
            "parseErrors": stats.get("parseErrors", 0),
            "warnings": stats.get("warnings", 0),
            "lastError": stats.get("lastError", ""),
            "lastWarning": stats.get("lastWarning", ""),
            "headerSeen": stats.get("headerSeen", False),
            "cellNames": stats.get("cellNames", []),
        }
