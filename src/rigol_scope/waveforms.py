"""Bounded synchronous WORD export; no background jobs or implicit resume."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import base64
import csv
import hashlib
import json
from pathlib import Path
import shutil
import struct
import time
import uuid

import numpy as np

from .controls import channel_id, enum, number
from .errors import ScopeError


@dataclass(frozen=True)
class Preamble:
    format: int
    mode: int
    points: int
    count: int
    x_increment: float
    x_origin: float
    x_reference: float
    y_increment: float
    y_origin: float
    y_reference: float

    @classmethod
    def parse(cls, value):
        try:
            parts = value.split(",")
            if len(parts) != 10: raise ValueError("expected 10 fields")
            first = [int(x) for x in parts[:4]]
            row = cls(*first, *(number(x) for x in parts[4:]))
            if row.format not in {0, 1, 2} or row.mode not in {0, 1, 2} or row.points < 1 or row.x_increment <= 0 or row.y_increment <= 0:
                raise ValueError("invalid waveform parameters")
            return row
        except (ValueError, TypeError) as exc:
            raise ScopeError(f"Invalid waveform preamble: {exc}") from exc

    def voltage(self, code):
        return (code - self.y_origin - self.y_reference) * self.y_increment

    def time(self, index):
        return (index - self.x_reference) * self.x_increment + self.x_origin


def decode_word(data: bytes, pre: Preamble, expected_points: int):
    if pre.format != 1 or len(data) != 2 * expected_points or pre.points != expected_points:
        raise ScopeError("WORD block/preamble length does not match the requested window")
    return struct.iter_unpack("<H", data)


def export_waveforms(driver, state_dir: Path, identity: dict, channels, mode="NORM", points=1000,
                     start=1, chunk_points=100000, stop_before_capture=False, run_after_capture=False,
                     max_bytes=268435456):
    if not channels or len(channels) > 4 or len(set(channels)) != len(channels):
        raise ScopeError("Choose 1–4 unique channels")
    for ch in channels: channel_id(ch)
    enum(mode, "mode", ["NORM", "RAW"])
    for name, value, maximum in [("points", points, 500000000), ("start", start, 500000000), ("chunk_points", chunk_points, 4000000), ("max_bytes", max_bytes, 2147483648)]:
        if type(value) is not int or not 1 <= value <= maximum: raise ScopeError(f"{name} must be an integer between 1 and {maximum}")
    if type(stop_before_capture) is not bool or type(run_after_capture) is not bool: raise ScopeError("capture state flags must be Boolean")
    if mode == "NORM" and start + points - 1 > 1000: raise ScopeError("NORM window must fit in 1..1000")
    # CSV is a small-capture convenience; binary remains the exact canonical data.
    csv_enabled = points <= 10000
    chunks = (points + chunk_points - 1) // chunk_points
    if chunks > 10000: raise ScopeError("Use larger chunks: too many metadata records for an interactive capture")
    estimated = len(channels) * (points * (100 if csv_enabled else 2) + chunks * 1024) + 1048576
    if estimated > max_bytes: raise ScopeError("Capture exceeds requested disk budget; increase budget or reduce window")
    state_dir.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(state_dir).free < estimated: raise ScopeError("Insufficient free space for capture budget")
    t = driver.transport
    before_state = t.query(":TRIGger:STATus?")
    if before_state != "STOP" and not stop_before_capture:
        raise ScopeError("Coherent export requires STOP; explicitly allow stop_before_capture or stop acquisition first")
    for ch in channels:
        if t.query(f":CHANnel{ch}:DISPlay?") != "1": raise ScopeError(f"Channel {ch} is not enabled")
    old = {k: t.query(":WAVeform:" + k + "?") for k in ["SOURce", "MODE", "FORMat", "STARt", "STOP"]}
    capture_id = uuid.uuid4().hex
    directory = state_dir / "captures" / capture_id
    directory.mkdir(parents=True, mode=0o700)
    metadata = {"schema_version": 2, "kind": "waveform", "capture_id": capture_id, "identity": identity,
                "created_at": datetime.now(timezone.utc).isoformat(), "status": "partial", "format": "WORD",
                "dtype": "uint16", "byteorder": "little", "mode": mode, "requested_points_per_channel": points,
                "start_index_one_based": start, "channels": {}, "acquisition_before": before_state,
                "waveform_settings_before": old, "max_bytes": max_bytes,
                "operation_timeout_s":getattr(driver,"operation_timeout_s",t.device.operation_timeout_s)}
    export_started = time.monotonic()
    binary_read_s = 0.0
    statistics_s = 0.0
    failure = None
    stopped_by_us = False
    horizontal = None
    restore_target = dict(old)
    try:
        if before_state != "STOP":
            t.write(":STOP")
            stopped_by_us = True
        if t.query(":TRIGger:STATus?") != "STOP": raise ScopeError("Instrument did not enter STOP")
        depth_response = t.query(":ACQuire:MDEPth?")
        metadata["memory_depth_response"] = depth_response
        old_limit = 1000 if old["MODE"]=="NORM" else (int(number(depth_response)) if depth_response.upper()!="AUTO" else 500000000)
        if not 1 <= int(old["STARt"]) <= int(old["STOP"]) <= old_limit:
            restore_target["STARt"]="1";restore_target["STOP"]=str(min(1000,old_limit))
            metadata["previous_transfer_window_invalid"]=True
        if mode == "RAW" and depth_response.upper() != "AUTO" and start + points - 1 > int(number(depth_response)):
            raise ScopeError("Requested RAW window exceeds current record depth")
        for channel in channels:
            t.write(f":WAVeform:SOURce CHANnel{channel}")
            # Reset the transfer window before RAW; stale bounds can exceed a newly reduced record.
            t.write(":WAVeform:MODE NORM")
            if t.query("*OPC?") != "1": raise ScopeError("Waveform window normalization did not complete")
            t.write(":WAVeform:MODE " + mode)
            t.write(":WAVeform:FORMat WORD")
            if t.query("*OPC?") != "1": raise ScopeError("Waveform source/mode setup did not complete")
            t.write(":WAVeform:STARt 1")
            if t.query("*OPC?") != "1": raise ScopeError("Waveform start reset did not complete")
            row = {"channel": channel, "points_written": 0, "bytes_written": 0, "preview": [], "chunks": []}
            metadata["channels"][str(channel)] = row
            digest = hashlib.sha256()
            csv_file = (directory / f"ch{channel}.csv").open("w", newline="") if csv_enabled else None
            writer = csv.writer(csv_file) if csv_file else None
            if writer: writer.writerow(["sample_index", "time_s", "raw_uint16", "voltage_v"])
            minimum, maximum, total = float("inf"), float("-inf"), 0.0
            baseline = None
            preview_stride = max(1, points // 128)
            previous_chunk_s = 0.0
            try:
                with (directory / f"ch{channel}.bin").open("wb") as binary:
                    for offset in range(0, points, chunk_points):
                        if getattr(driver,"export_cancelled",lambda:False)():raise ScopeError("Prepared export cancelled before next block")
                        t.remaining()
                        chunk_started = time.monotonic()
                        if getattr(t,"deadline",float("inf"))-chunk_started < max(3.0,(previous_chunk_s*1.5 if previous_chunk_s else min(chunk_points,points-offset)*2/1000000+1)+3):
                            raise ScopeError("Export time budget too short for another block; partial record retained, transfer cleanup reserved")
                        count = min(chunk_points, points - offset)
                        first = start + offset
                        last = first + count - 1
                        if count * 2 > t.device.max_binary_bytes: raise ScopeError("Chunk exceeds configured transport byte limit")
                        if t.query(":TRIGger:STATus?") != "STOP": raise ScopeError("Acquisition changed during export")
                        t.write(f":WAVeform:STOP {last}")
                        t.write(f":WAVeform:STARt {first}")
                        if t.query("*OPC?") != "1": raise ScopeError("Waveform window setup did not complete")
                        actual_start=int(t.query(":WAVeform:STARt?"));actual_stop=int(t.query(":WAVeform:STOP?"))
                        if actual_start != first or actual_stop != last:
                            raise ScopeError(f"Instrument changed waveform window: requested {first}..{last}, actual {actual_start}..{actual_stop}")
                        pre = Preamble.parse(t.query(":WAVeform:PREamble?"))
                        if pre.mode != (0 if mode == "NORM" else 2): raise ScopeError("Unexpected waveform mode")
                        scale = (pre.x_increment, pre.x_origin, pre.x_reference, pre.y_increment, pre.y_origin, pre.y_reference)
                        if baseline is not None and baseline != scale: raise ScopeError("Waveform scale/time parameters changed between chunks")
                        baseline = scale
                        if horizontal is None: horizontal = scale[:3]
                        elif horizontal != scale[:3]: raise ScopeError("Channels do not share the same horizontal sampling")
                        block_started = time.monotonic()
                        payload = t.query_binary(":WAVeform:DATA?")
                        binary_read_s += time.monotonic() - block_started
                        codes = decode_word(payload, pre, count)
                        # Require a stable stopped record for every block, not only before it.
                        if t.query(":TRIGger:STATus?") != "STOP": raise ScopeError("Acquisition changed while reading a block")
                        binary.write(payload); digest.update(payload)
                        stats_started = time.monotonic()
                        if writer:
                            for index, (code,) in enumerate(codes):
                                absolute = first - 1 + index
                                volts = pre.voltage(code)
                                minimum = min(minimum, volts); maximum = max(maximum, volts); total += volts
                                writer.writerow([absolute, pre.time(absolute), code, volts])
                                if (offset + index) % preview_stride == 0 and len(row["preview"]) < 128:
                                    row["preview"].append([pre.time(absolute), volts])
                        else:
                            raw_codes = np.frombuffer(payload, dtype="<u2")
                            minimum = min(minimum, pre.voltage(int(raw_codes.min())))
                            maximum = max(maximum, pre.voltage(int(raw_codes.max())))
                            total += (int(raw_codes.sum(dtype=np.uint64)) - count * (pre.y_origin + pre.y_reference)) * pre.y_increment
                            for index in range((-offset) % preview_stride, count, preview_stride):
                                if len(row["preview"]) >= 128: break
                                row["preview"].append([pre.time(first - 1 + index), pre.voltage(int(raw_codes[index]))])
                        statistics_s += time.monotonic() - stats_started
                        row["points_written"] += count
                        row["bytes_written"] += len(payload)
                        row["chunks"].append({"start": first, "stop": last, "preamble": asdict(pre)})
                        previous_chunk_s = time.monotonic() - chunk_started
            finally:
                if csv_file: csv_file.close()
                row["sha256"] = digest.hexdigest()
            row.update(min_v=minimum, max_v=maximum, mean_v=total / points, time_start_s=pre.time(start - 1), time_end_s=pre.time(start + points - 2), file=f"ch{channel}.bin")
        metadata["status"] = "complete"
    except Exception as exc:
        failure = exc
        metadata["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        try:
            t.write(":WAVeform:MODE NORM")
            if t.query("*OPC?") != "1": raise ScopeError("Transfer cleanup normalization did not complete")
            for key in ["SOURce", "MODE", "FORMat"]:
                t.write(":WAVeform:" + key + " " + restore_target[key])
            if t.query("*OPC?") != "1": raise ScopeError("Transfer cleanup mode did not complete")
            t.write(":WAVeform:STARt 1")
            t.write(":WAVeform:STOP " + restore_target["STOP"])
            t.write(":WAVeform:STARt " + restore_target["STARt"])
            if stopped_by_us and run_after_capture:
                driver.acquisition_control("run")
            restored = {k: t.query(":WAVeform:" + k + "?") for k in old}
            metadata["restoration"] = {"verified": restored == restore_target, "actual": restored,
                                       "previous_settings_restored":restored==old,"expected":restore_target,
                                       "previous_window_invalid":metadata.get("previous_transfer_window_invalid",False)}
            metadata["acquisition_after"] = t.query(":TRIGger:STATus?")
        except Exception as exc:
            metadata["restoration"] = {"verified": False, "error": str(exc), "note": "No automatic reconnection or re-arm attempted"}
        metadata["performance"] = {"export_elapsed_s":round(time.monotonic()-export_started,4),
                                   "binary_read_s":round(binary_read_s,4),"statistics_s":round(statistics_s,4),
                                   "bytes_written":sum(r["bytes_written"] for r in metadata["channels"].values()),
                                   "chunk_points":chunk_points,"statistics_method":"numpy uint16/uint64 streaming; exact source bytes preserved"}
        temporary = directory / "metadata.json.partial"
        temporary.write_text(json.dumps(metadata, indent=2) + "\n")
        temporary.replace(directory / "metadata.json")
    summary = {"capture_id": capture_id, "directory": str(directory), "metadata_uri": f"scope://artifacts/{capture_id}/metadata.json",
               "performance": metadata["performance"], "status": metadata["status"], "verified": failure is None and metadata["restoration"]["verified"],
               "restoration": metadata["restoration"], "acquisition_after": metadata.get("acquisition_after"),
               "channels": {k: {key: value for key, value in row.items() if key != "chunks"} for k, row in metadata["channels"].items()}}
    if failure: summary["error"] = metadata["error"]
    return summary
