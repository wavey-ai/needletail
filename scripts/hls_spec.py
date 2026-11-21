#!/usr/bin/env python3
"""Check LL-HLS playlists and the HTTP behaviour behind them against
draft-pantos-hls-rfc8216bis-22 and Apple's HLS authoring specification.

The checks follow the numbered list in docs/media-edge-gaps.md, "LL-HLS
conformance". Each check is a MUST (`fail`) or a SHOULD (`warn`), and names
the section of the draft, or the rule of the authoring specification.

As a module:
    playlist = parse_media(text)
    findings = check_media(playlist)
    findings += check_reload(previous, playlist)
    findings += check_http(fetch, "/live/1/stream.m3u8")     # fetch(path, **kw)

From the command line, against a node of the edge lab:
    hls_spec.py lab <node> <stream> [--seconds 20]
"""

from __future__ import annotations

import datetime
import json
import math
import sys
import time


class Finding(dict):
    def __init__(self, rule: int, level: str, text: str, reference: str):
        super().__init__(rule=rule, level=level, text=text, reference=reference)


def attributes(line: str) -> dict:
    """The attribute list of a tag, with quoted strings unquoted."""
    _, _, rest = line.partition(":")
    out = {}
    key = ""
    value = ""
    in_key = True
    quoted = False
    for char in rest:
        if in_key:
            if char == "=":
                in_key = False
            elif char != ",":
                key += char
        elif char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            out[key.strip()] = value
            key, value, in_key = "", "", True
        else:
            value += char
    if key.strip():
        out[key.strip()] = value
    return out


def parse_time(value: str) -> float | None:
    try:
        text = value.strip().replace("Z", "+00:00")
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def parse_media(text: str) -> dict:
    """A Media Playlist as a dictionary. Segments carry their parts. The
    parts after the last segment belong to the segment that is still open."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    playlist = {
        "raw_lines": lines, "first_line": lines[0] if lines else "", "versions": [],
        "target_duration": None, "media_sequence": None, "media_sequence_line": None,
        "discontinuity_sequence": None, "part_target": None, "server_control": None,
        "server_control_count": 0, "segments": [], "open_parts": [], "preload_hints": [],
        "rendition_reports": [], "skip": None, "skip_count": 0, "endlist": False,
        "playlist_type": None, "independent_segments": 0, "maps": 0, "errors": [],
        "first_segment_line": None,
    }
    pending = {"parts": [], "duration": None, "pdt": None, "map": None, "gap": False,
               "discontinuity": False, "tags_after_first_part": []}
    current_map = None
    msn = None
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-VERSION:"):
            playlist["versions"].append(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            playlist["target_duration"] = line.split(":", 1)[1]
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            playlist["media_sequence"] = int(line.split(":", 1)[1])
            playlist["media_sequence_line"] = index
            msn = playlist["media_sequence"]
        elif line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
            playlist["discontinuity_sequence"] = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-PLAYLIST-TYPE:"):
            playlist["playlist_type"] = line.split(":", 1)[1]
        elif line == "#EXT-X-INDEPENDENT-SEGMENTS":
            playlist["independent_segments"] += 1
        elif line.startswith("#EXT-X-PART-INF:"):
            try:
                playlist["part_target"] = float(attributes(line).get("PART-TARGET", "nan"))
            except ValueError:
                playlist["errors"].append("PART-TARGET is not a number")
        elif line.startswith("#EXT-X-SERVER-CONTROL:"):
            playlist["server_control"] = attributes(line)
            playlist["server_control_count"] += 1
        elif line.startswith("#EXT-X-MAP:"):
            current_map = attributes(line).get("URI")
            playlist["maps"] += 1
            if pending["parts"]:
                pending["tags_after_first_part"].append("EXT-X-MAP")
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pending["pdt_text"] = line.split(":", 1)[1]
            pending["pdt"] = parse_time(pending["pdt_text"])
            if pending["parts"]:
                pending["tags_after_first_part"].append("EXT-X-PROGRAM-DATE-TIME")
        elif line == "#EXT-X-DISCONTINUITY":
            pending["discontinuity"] = True
            if pending["parts"]:
                pending["tags_after_first_part"].append("EXT-X-DISCONTINUITY")
        elif line == "#EXT-X-GAP":
            pending["gap"] = True
        elif line.startswith("#EXT-X-PART:"):
            item = attributes(line)
            part = {"uri": item.get("URI"), "independent": item.get("INDEPENDENT") == "YES",
                    "gap": item.get("GAP") == "YES", "byterange": item.get("BYTERANGE"),
                    "map": current_map}
            try:
                part["duration"] = float(item["DURATION"])
            except (KeyError, ValueError):
                part["duration"] = None
            pending["parts"].append(part)
        elif line.startswith("#EXTINF:"):
            try:
                pending["duration"] = float(line.split(":", 1)[1].split(",")[0])
            except ValueError:
                playlist["errors"].append(f"EXTINF is not a number: {line}")
        elif line.startswith("#EXT-X-PRELOAD-HINT:"):
            playlist["preload_hints"].append(attributes(line))
        elif line.startswith("#EXT-X-RENDITION-REPORT:"):
            playlist["rendition_reports"].append(attributes(line))
        elif line.startswith("#EXT-X-SKIP:"):
            playlist["skip"] = attributes(line)
            playlist["skip_count"] += 1
            try:
                msn = (msn or 0) + int(playlist["skip"].get("SKIPPED-SEGMENTS", "0"))
            except ValueError:
                playlist["errors"].append("SKIPPED-SEGMENTS is not a number")
        elif line == "#EXT-X-ENDLIST":
            playlist["endlist"] = True
        elif not line.startswith("#"):
            if playlist["first_segment_line"] is None:
                playlist["first_segment_line"] = index
            playlist["segments"].append({
                "msn": msn, "uri": line, "duration": pending["duration"],
                "pdt": pending["pdt"], "pdt_text": pending.get("pdt_text"),
                "map": current_map, "gap": pending["gap"],
                "discontinuity": pending["discontinuity"], "parts": pending["parts"],
                "tags_after_first_part": pending["tags_after_first_part"]})
            msn = (msn or 0) + 1
            pending = {"parts": [], "duration": None, "pdt": None, "map": None, "gap": False,
                       "discontinuity": False, "tags_after_first_part": []}
    playlist["open_parts"] = pending["parts"]
    playlist["open_pdt"] = pending["pdt"]
    playlist["open_tags_after_first_part"] = pending["tags_after_first_part"]
    playlist["next_msn"] = msn
    return playlist


def last_position(playlist: dict) -> tuple[int, int] | None:
    """(Media Sequence Number, part index) of the newest part."""
    if playlist["open_parts"]:
        return playlist["next_msn"], len(playlist["open_parts"]) - 1
    for segment in reversed(playlist["segments"]):
        if segment["parts"]:
            return segment["msn"], len(segment["parts"]) - 1
    return None


def all_parts(playlist: dict) -> list[dict]:
    return [part for segment in playlist["segments"] for part in segment["parts"]] + \
        playlist["open_parts"]


def check_media(playlist: dict, video: bool = True, fmp4: bool = True,
                frame_seconds: float = 0.05) -> list[Finding]:
    """The checks that one copy of a Media Playlist can fail."""
    out = []

    def fail(rule, text, reference):
        out.append(Finding(rule, "fail", text, reference))

    def warn(rule, text, reference):
        out.append(Finding(rule, "warn", text, reference))

    for error in playlist["errors"]:
        fail(0, error, "syntax")
    if playlist["first_line"] != "#EXTM3U":
        fail(1, "the first line is not #EXTM3U", "4.4.1.1")
    if len(playlist["versions"]) > 1:
        fail(1, "more than one EXT-X-VERSION", "4.4.1.2")
    version = int(playlist["versions"][0]) if playlist["versions"] else 1
    if playlist["maps"] and version < 6:
        fail(2, f"EXT-X-MAP needs version 6, the playlist says {version}", "8")
    if playlist["skip"] and version < 9:
        fail(2, f"EXT-X-SKIP needs version 9, the playlist says {version}", "8")
    try:
        target = int(playlist["target_duration"])
    except (TypeError, ValueError):
        fail(3, f"EXT-X-TARGETDURATION is missing or not an integer: "
                f"{playlist['target_duration']}", "4.4.3.1")
        target = None
    segments = playlist["segments"]
    if target is not None:
        for segment in segments:
            if segment["duration"] is not None and round(segment["duration"]) > target:
                fail(4, f"segment {segment['msn']} lasts {segment['duration']} s, more than "
                        f"the target duration {target}", "4.4.3.1")
                break
        for segment in segments:
            if segment["duration"] is not None and segment["duration"] > target + 0.5:
                warn(4, f"segment {segment['msn']} lasts {segment['duration']} s, more than "
                        f"the target duration {target} + 0.5", "Apple 7.7")
                break
    if playlist["media_sequence"] is None:
        fail(5, "EXT-X-MEDIA-SEQUENCE is missing from a live playlist", "6.2.2")
    elif (playlist["first_segment_line"] is not None
          and playlist["media_sequence_line"] > playlist["first_segment_line"]):
        fail(5, "EXT-X-MEDIA-SEQUENCE comes after the first segment", "4.4.3.2")
    if playlist["playlist_type"] and not playlist["endlist"]:
        fail(6, "EXT-X-PLAYLIST-TYPE in a live playlist that removes segments", "6.2.2")
    if playlist["independent_segments"] > 1:
        fail(7, "EXT-X-INDEPENDENT-SEGMENTS appears more than once", "4.4.2")
    if playlist["skip_count"] > 1:
        fail(7, "EXT-X-SKIP appears more than once", "4.4.5.2")
    if fmp4:
        unmapped = [segment["msn"] for segment in segments if not segment["map"]]
        unmapped_parts = [part["uri"] for part in all_parts(playlist) if not part["map"]]
        if unmapped or unmapped_parts:
            fail(8, f"{len(unmapped)} segments and {len(unmapped_parts)} parts are outside "
                    "the scope of an EXT-X-MAP", "3.1.2; Apple 8.20")
    if target is not None and not playlist["endlist"] and not playlist["skip"]:
        total = sum(segment["duration"] or 0 for segment in segments)
        if total < 3 * target:
            # The rule of the draft is about removal: a server MUST NOT
            # remove a segment when that leaves less than three target
            # durations. `check_reload` sees a removal. A playlist that is
            # short because it is new, a stream or a copy that has just
            # begun, breaks no rule, and a player waits for more of it.
            warn(9, f"the playlist holds {total:.1f} s of segments, less than three target "
                    f"durations ({3 * target} s)", "6.2.2")
        if len(segments) < 6:
            warn(9, f"the playlist holds {len(segments)} segments, fewer than six",
                 "Apple 8.11")

    # Parts
    parts = all_parts(playlist)
    part_target = playlist["part_target"]
    if bool(parts) != (part_target is not None):
        fail(10, "EXT-X-PART-INF and EXT-X-PART are not present together", "4.4.3.7")
    if parts and part_target is not None and not math.isnan(part_target):
        missing = [part for part in parts if part["uri"] is None or part["duration"] is None]
        if missing:
            fail(11, f"{len(missing)} parts lack URI or DURATION", "4.4.4.9")
        long = [part for part in parts
                if part["duration"] is not None and part["duration"] > part_target + 1e-6]
        if long:
            fail(12, f"{len(long)} of {len(parts)} parts are longer than PART-TARGET "
                     f"{part_target}: the first is {long[0]['duration']} s ({long[0]['uri']})",
                 "4.4.4.9")
        short = 0
        first_short = None
        groups = [segment["parts"] for segment in segments] + [playlist["open_parts"]]
        for group_index, group in enumerate(groups):
            is_open = group_index == len(groups) - 1
            for index, part in enumerate(group):
                last_of_segment = index == len(group) - 1 and not is_open
                followed_by_gap = index + 1 < len(group) and group[index + 1]["gap"]
                if (part["duration"] is not None
                        and part["duration"] < 0.85 * part_target - 1e-6
                        and not part["independent"] and not part["gap"]
                        and not last_of_segment and not followed_by_gap):
                    short += 1
                    first_short = first_short or part
        if short:
            fail(13, f"{short} of {len(parts)} parts are shorter than 85% of PART-TARGET "
                     f"{part_target} and are not the last of their segment: the first is "
                     f"{first_short['duration']} s ({first_short['uri']})", "4.4.4.9")
        for segment in segments:
            if segment["parts"] and segment["duration"] is not None:
                total = sum(part["duration"] or 0 for part in segment["parts"])
                if abs(total - segment["duration"]) > frame_seconds + 1e-6:
                    fail(14, f"the parts of segment {segment['msn']} last {total:.3f} s and "
                             f"its EXTINF is {segment['duration']:.3f} s", "3.2; Apple 8.1")
                    break
        misplaced = [(segment["msn"], segment["tags_after_first_part"])
                     for segment in segments if segment["tags_after_first_part"]]
        if playlist["open_tags_after_first_part"]:
            misplaced.append((playlist["next_msn"], playlist["open_tags_after_first_part"]))
        if misplaced:
            fail(15, f"tags of a segment come after its first part: {misplaced[:2]}",
                 "4.4.4.9")
        if target is not None:
            # Parts SHOULD be listed only near the live edge.
            age = sum(part["duration"] or 0 for part in playlist["open_parts"])
            old = 0
            for segment in reversed(segments):
                if age > 3 * target + (segment["duration"] or 0) and segment["parts"]:
                    old += 1
                age += segment["duration"] or 0
            if old:
                warn(16, f"{old} segments older than three target durations still list "
                         "their parts", "6.2.2")
        if video:
            without = [segment["msn"] for segment in segments
                       if segment["parts"] and not any(part["independent"]
                                                       for part in segment["parts"])]
            if without:
                warn(17, f"{len(without)} segments have no part with INDEPENDENT=YES",
                     "4.4.4.9; B.1")
            not_first = [segment["msn"] for segment in segments
                         if segment["parts"] and not segment["parts"][0]["independent"]]
            if not_first:
                warn(17, f"{len(not_first)} segments do not start with an independent part",
                     "Apple 7.4")

    # Server control
    control = playlist["server_control"]
    if part_target is not None:
        if control is None or "PART-HOLD-BACK" not in control:
            fail(19, "EXT-X-SERVER-CONTROL with PART-HOLD-BACK is missing", "4.4.3.8")
        else:
            hold = float(control["PART-HOLD-BACK"])
            if hold < 2 * part_target - 1e-6:
                fail(20, f"PART-HOLD-BACK {hold} is less than twice PART-TARGET "
                         f"{part_target}", "4.4.3.8")
            elif hold < 3 * part_target - 1e-6:
                warn(20, f"PART-HOLD-BACK {hold} is less than three times PART-TARGET "
                         f"{part_target}", "4.4.3.8; Apple 14.3")
        if control is not None and control.get("CAN-BLOCK-RELOAD") != "YES":
            fail(24, "CAN-BLOCK-RELOAD=YES is missing from a low-latency playlist",
                 "6.2.5.2")
        if part_target < 0.2:
            warn(26, f"PART-TARGET {part_target} s is below any round trip that a reader "
                     "on the Internet has", "Apple 14.2")
    if control is not None and target is not None:
        if "HOLD-BACK" in control and float(control["HOLD-BACK"]) < 3 * target - 1e-6:
            fail(21, f"HOLD-BACK {control['HOLD-BACK']} is less than three target durations",
                 "4.4.3.8")
        if "CAN-SKIP-UNTIL" in control and float(control["CAN-SKIP-UNTIL"]) < 6 * target - 1e-6:
            fail(22, f"CAN-SKIP-UNTIL {control['CAN-SKIP-UNTIL']} is less than six target "
                     "durations", "4.4.3.8")
        if "CAN-SKIP-DATERANGES" in control and "CAN-SKIP-UNTIL" not in control:
            fail(23, "CAN-SKIP-DATERANGES without CAN-SKIP-UNTIL", "4.4.3.8")
    if playlist["server_control_count"] > 1:
        fail(19, "EXT-X-SERVER-CONTROL appears more than once", "4.4.3.8")

    # Preload hints
    hints = [hint for hint in playlist["preload_hints"] if hint.get("TYPE") == "PART"]
    if part_target is not None and not playlist["endlist"] and len(hints) != 1:
        fail(27, f"a live playlist with parts has {len(hints)} EXT-X-PRELOAD-HINT of "
                 "TYPE=PART; it needs one", "B.1; 6.2.6")
    if playlist["endlist"] and hints:
        fail(27, "EXT-X-PRELOAD-HINT in a playlist with EXT-X-ENDLIST", "4.4.5.3")

    # Dates
    with_date = [segment for segment in segments if segment["pdt"] is not None]
    if not with_date and playlist["open_pdt"] is None:
        fail(58, "EXT-X-PROGRAM-DATE-TIME is missing", "B.1; Apple 8.4")
    for segment in segments:
        text = segment.get("pdt_text")
        if text and ("." not in text or not (text.endswith("Z") or "+" in text[10:]
                                              or "-" in text[10:])):
            fail(58, f"EXT-X-PROGRAM-DATE-TIME {text} lacks milliseconds or a time zone",
                 "4.4.4.6")
            break
    previous = None
    for segment in segments:
        if segment["pdt"] is not None and previous is not None and not segment["discontinuity"]:
            expected = previous["pdt"] + previous["elapsed"]
            if abs(segment["pdt"] - expected) > max(0.2, frame_seconds * 2):
                fail(59, f"the date of segment {segment['msn']} is "
                         f"{segment['pdt'] - expected:+.3f} s away from the date of segment "
                         f"{previous['msn']} and the durations between them, without "
                         "EXT-X-DISCONTINUITY or EXT-X-GAP", "6.2.1")
                break
        if segment["pdt"] is not None:
            previous = {"pdt": segment["pdt"], "elapsed": 0.0, "msn": segment["msn"]}
        if previous is not None:
            previous["elapsed"] += segment["duration"] or 0
    if any(segment["discontinuity"] for segment in segments) and \
            playlist["discontinuity_sequence"] is None:
        warn(63, "EXT-X-DISCONTINUITY without EXT-X-DISCONTINUITY-SEQUENCE",
             "6.2.2; Apple 8.17")
    if video and playlist["independent_segments"] == 0:
        warn(64, "EXT-X-INDEPENDENT-SEGMENTS is missing from a video playlist",
             "Apple 9.11")
    return out


def check_reload(before: dict, after: dict, seconds_between: float | None = None
                 ) -> list[Finding]:
    """The checks between two copies of one playlist, the older first."""
    out = []
    if before["target_duration"] != after["target_duration"]:
        out.append(Finding(54, "fail", f"EXT-X-TARGETDURATION changed from "
                           f"{before['target_duration']} to {after['target_duration']}",
                           "6.2.1"))
    if (before["media_sequence"] is not None and after["media_sequence"] is not None
            and after["media_sequence"] < before["media_sequence"]):
        out.append(Finding(52, "fail", f"EXT-X-MEDIA-SEQUENCE went back from "
                           f"{before['media_sequence']} to {after['media_sequence']}", "6.2.2"))
    if (before["media_sequence"] is not None and after["media_sequence"] is not None
            and after["media_sequence"] > before["media_sequence"]
            and not after["endlist"] and not after["skip"]):
        try:
            target = float(after["target_duration"])
        except (TypeError, ValueError):
            target = None
        total = sum(segment["duration"] or 0 for segment in after["segments"])
        if target is not None and total < 3 * target:
            out.append(Finding(9, "fail", f"a segment was removed and the playlist now holds "
                               f"{total:.1f} s, less than three target durations "
                               f"({3 * target} s)", "6.2.2"))
    old = {segment["msn"]: segment for segment in before["segments"]}
    for segment in after["segments"]:
        earlier = old.get(segment["msn"])
        if earlier is None:
            continue
        if earlier["uri"] != segment["uri"]:
            out.append(Finding(52, "fail", f"segment {segment['msn']} changed its URI from "
                               f"{earlier['uri']} to {segment['uri']}", "6.3.4"))
            break
        if (earlier["duration"] is not None and segment["duration"] is not None
                and abs(earlier["duration"] - segment["duration"]) > 1e-6):
            out.append(Finding(53, "fail", f"segment {segment['msn']} changed its duration "
                               f"from {earlier['duration']} to {segment['duration']}",
                               "6.2.1"))
            break
        if earlier["pdt"] is not None and segment["pdt"] is not None and \
                abs(earlier["pdt"] - segment["pdt"]) > 0.0015:
            out.append(Finding(53, "fail", f"segment {segment['msn']} changed its date by "
                               f"{segment['pdt'] - earlier['pdt']:+.3f} s", "6.2.1"))
            break
    # The hinted part is the next part of the playlist.
    hints = [hint for hint in before["preload_hints"] if hint.get("TYPE") == "PART"]
    if hints:
        known = {part["uri"] for part in all_parts(before)}
        added = [part["uri"] for part in all_parts(after) if part["uri"] not in known]
        if added and added[0] != hints[0].get("URI"):
            out.append(Finding(28, "fail", f"the playlist hinted {hints[0].get('URI')} and "
                               f"then added {added[0]}", "4.4.5.3"))
    return out


def check_http(fetch, path: str, playlist: dict | None = None, video: bool = True
               ) -> list[Finding]:
    """The checks of the HTTP behaviour behind a Media Playlist.

    `fetch(path, headers=None, timeout=10)` returns (status, headers, body,
    seconds), with header names in lower case."""
    out = []

    def fail(rule, text, reference):
        out.append(Finding(rule, "fail", text, reference))

    def warn(rule, text, reference):
        out.append(Finding(rule, "warn", text, reference))

    status, headers, body, _ = fetch(path)
    if status != 200:
        fail(0, f"the playlist answered {status}", "")
        return out
    playlist = parse_media(body)
    base = path.rsplit("/", 1)[0] + "/"
    target = int(playlist["target_duration"] or 1)
    position = last_position(playlist)
    content_type = headers.get("content-type", "")
    if "mpegurl" not in content_type.lower():
        fail(70, f"the playlist has Content-Type {content_type!r}", "Apple 10.4")
    if "cache-control" not in headers:
        warn(71, "the playlist has no Cache-Control", "B.1")
    _, gz_headers, _, _ = fetch(path, headers={"accept-encoding": "gzip"})
    if "gzip" not in gz_headers.get("content-encoding", ""):
        fail(69, "the playlist is not compressed for a client that accepts gzip",
             "B.1; Apple 10.1")
    if playlist["part_target"] is not None and position is not None:
        msn, part = position
        # 34: a blocking reload for the next part.
        started = time.time()
        status, _, blocked, seconds = fetch(f"{path}?_HLS_msn={msn}&_HLS_part={part + 1}",
                                            timeout=3 * target + 4)
        if status != 200:
            fail(34, f"a blocking reload for the next part answered {status}", "6.2.5.2")
        else:
            newer = last_position(parse_media(blocked))
            if newer is None or newer <= (msn, part):
                fail(34, f"a blocking reload for part {part + 1} of segment {msn} returned a "
                         f"playlist that ends at {newer}", "6.2.5.2")
        # 35: a blocking reload for the next segment.
        status, _, blocked, seconds = fetch(f"{path}?_HLS_msn={msn}", timeout=3 * target + 4)
        if status != 200:
            fail(35, f"a blocking reload for segment {msn} answered {status}", "6.2.5.2")
        else:
            newer = parse_media(blocked)
            if not any(segment["msn"] >= msn for segment in newer["segments"]):
                fail(35, f"a blocking reload for segment {msn} returned a playlist whose "
                         f"last complete segment is "
                         f"{newer['segments'][-1]['msn'] if newer['segments'] else None}",
                     "6.2.5.2")
        # 38-40: requests that a server refuses at once.
        status, _, _, seconds = fetch(f"{path}?_HLS_part=0", timeout=3 * target + 4)
        if status != 400:
            fail(38, f"_HLS_part without _HLS_msn answered {status} after {seconds:.1f} s, "
                     "not 400", "6.2.5.2")
        current = last_position(parse_media(fetch(path)[2]))
        if current is not None:
            status, _, _, seconds = fetch(f"{path}?_HLS_msn={current[0] + 3}",
                                          timeout=3 * target + 4)
            if status != 400 or seconds > 1.0:
                fail(39, f"a reload for a segment three ahead answered {status} after "
                         f"{seconds:.1f} s, not 400 at once", "6.2.5.2")
            advance = math.ceil(3 / playlist["part_target"]) if playlist["part_target"] < 1 \
                else 3
            status, _, _, seconds = fetch(
                f"{path}?_HLS_msn={current[0]}&_HLS_part={current[1] + advance + 50}",
                timeout=3 * target + 4)
            if status != 400 or seconds > 1.0:
                warn(40, f"a reload for a part beyond the advance part limit answered "
                         f"{status} after {seconds:.1f} s, not 400 at once", "6.2.5.2")
        # 43: a request for an old position returns at once.
        status, _, _, seconds = fetch(f"{path}?_HLS_msn={max(0, msn - 1)}&_HLS_part=0")
        if status != 200 or seconds > 0.5:
            fail(43, f"a reload for an old part answered {status} after {seconds:.1f} s",
                 "6.2.5.2")
        # 29: the hinted part can be asked for at once.
        hints = [hint for hint in playlist["preload_hints"] if hint.get("TYPE") == "PART"]
        if hints:
            status, part_headers, part_body, seconds = fetch(base + hints[0]["URI"],
                                                             timeout=3 * target + 4)
            if status != 200 or not part_body:
                fail(29, f"the hinted part {hints[0]['URI']} answered {status}", "6.2.6")
        # 45: delta updates.
        control = playlist["server_control"] or {}
        status, _, delta, _ = fetch(f"{path}?_HLS_skip=YES")
        if "CAN-SKIP-UNTIL" in control:
            if status != 200 or "#EXT-X-SKIP:" not in delta:
                fail(45, "the playlist offers CAN-SKIP-UNTIL and _HLS_skip=YES returned no "
                         "EXT-X-SKIP", "6.2.5.1")
        else:
            warn(45, "the playlist offers no delta update (CAN-SKIP-UNTIL)",
                 "6.2.5.1; Apple 8.12")
            if "#EXT-X-SKIP:" in delta:
                fail(49, "_HLS_skip was honoured without CAN-SKIP-UNTIL", "6.2.5.1")
    # 52/55: every listed resource can be fetched.
    missing = []
    for segment in playlist["segments"][-3:]:
        status, seg_headers, _, _ = fetch(base + segment["uri"], timeout=10)
        if status != 200:
            missing.append(f"{segment['uri']}: {status}")
    if missing:
        fail(55, f"segments in the playlist cannot be fetched: {missing}", "6.2.2")
    maps = {segment["map"] for segment in playlist["segments"] if segment["map"]}
    for uri in maps:
        status, map_headers, _, _ = fetch(base + uri)
        if status != 200:
            fail(8, f"the initialization section {uri} answered {status}", "3.1.2")
    parts = all_parts(playlist)
    if parts:
        status, part_headers, _, _ = fetch(base + parts[-1]["uri"])
        if status != 200:
            fail(55, f"the newest part {parts[-1]['uri']} answered {status}", "6.2.2")
        else:
            kind = part_headers.get("content-type", "")
            if not any(word in kind for word in ("mp4", "iso.segment", "mp2t")):
                warn(70, f"a part has Content-Type {kind!r}", "Apple 10.4")
            if "cache-control" not in part_headers:
                warn(71, "a part has no Cache-Control", "B.1")
    # 44: an unknown resource is not held.
    status, _, _, seconds = fetch(base + "part999999999.mp4", timeout=3 * target + 4)
    if status != 404 or seconds > 1.0:
        fail(44, f"an unknown part answered {status} after {seconds:.1f} s, not 404 at once",
             "6.2.6")
    return out


def check_multivariant(text: str) -> list[Finding]:
    """The checks of a Multivariant Playlist."""
    out = []
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    variants = []
    media = []
    steering = None
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            uri = lines[index + 1] if index + 1 < len(lines) else ""
            variants.append((attributes(line), uri))
        elif line.startswith("#EXT-X-MEDIA:"):
            media.append(attributes(line))
        elif line.startswith("#EXT-X-CONTENT-STEERING:"):
            steering = attributes(line)
    if not variants:
        out.append(Finding(100, "fail", "the playlist has no EXT-X-STREAM-INF", "4.4.6.2"))
        return out
    pathways = {item.get("PATHWAY-ID", ".") for item, _ in variants}
    for item, uri in variants:
        if "BANDWIDTH" not in item:
            out.append(Finding(101, "fail", f"{uri}: BANDWIDTH is missing", "4.4.6.2"))
        if "CODECS" not in item:
            out.append(Finding(102, "warn", f"{uri}: CODECS is missing", "Apple 9.1"))
        if "AVERAGE-BANDWIDTH" not in item:
            out.append(Finding(103, "warn", f"{uri}: AVERAGE-BANDWIDTH is missing",
                               "Apple 9.14"))
        has_video = any(codec.strip().startswith(("avc", "hvc", "hev", "av01", "vp0"))
                        for codec in item.get("CODECS", "").split(","))
        if has_video and "RESOLUTION" not in item:
            out.append(Finding(104, "warn", f"{uri}: RESOLUTION is missing", "Apple 9.2"))
        if has_video and "FRAME-RATE" not in item:
            out.append(Finding(105, "warn", f"{uri}: FRAME-RATE is missing", "Apple 9.15"))
    by_pathway = {}
    for item, _ in variants:
        by_pathway.setdefault(item.get("PATHWAY-ID", "."), set()).add(item.get("BANDWIDTH"))
    if all(len(rates) < 2 for rates in by_pathway.values()):
        out.append(Finding(106, "warn", "the playlist offers one bit rate: no ladder",
                           "Apple 9.9"))
    if len({frozenset(rates) for rates in by_pathway.values()}) > 1:
        out.append(Finding(107, "fail", "the pathways do not offer the same variants",
                           "B.1; steering"))
    if steering is not None:
        if "SERVER-URI" not in steering:
            out.append(Finding(110, "fail", "EXT-X-CONTENT-STEERING lacks SERVER-URI",
                               "4.4.6.6"))
        if "PATHWAY-ID" not in steering:
            out.append(Finding(111, "warn", "EXT-X-CONTENT-STEERING lacks PATHWAY-ID",
                               "Apple 9.18"))
        elif steering["PATHWAY-ID"] not in pathways:
            out.append(Finding(111, "fail", f"the initial pathway {steering['PATHWAY-ID']} "
                               "is not a pathway of a variant", "4.4.6.6"))
    groups = {item.get("AUDIO") for item, _ in variants if item.get("AUDIO")}
    declared = {item.get("GROUP-ID") for item in media if item.get("TYPE") == "AUDIO"}
    if groups - declared:
        out.append(Finding(112, "fail", f"audio groups {sorted(groups - declared)} are named "
                           "and not declared", "4.4.6.2"))
    for item in media:
        if item.get("TYPE") != "VIDEO" and "LANGUAGE" not in item:
            out.append(Finding(113, "warn", f"EXT-X-MEDIA {item.get('NAME')} lacks LANGUAGE",
                               "Apple 8.10"))
    return out


def check_steering(text: str, pathways: set[str]) -> list[Finding]:
    """The checks of a steering manifest (draft-pantos-content-steering)."""
    out = []
    try:
        manifest = json.loads(text)
    except ValueError:
        return [Finding(120, "fail", "the steering manifest is not JSON", "CS 4")]
    if manifest.get("VERSION") != 1:
        out.append(Finding(120, "fail", f"VERSION is {manifest.get('VERSION')!r}", "CS 4"))
    if not isinstance(manifest.get("TTL"), int):
        out.append(Finding(121, "fail", "TTL is missing or not an integer", "CS 4"))
    priority = manifest.get("PATHWAY-PRIORITY")
    if not isinstance(priority, list) or not priority:
        out.append(Finding(122, "fail", "PATHWAY-PRIORITY is missing or empty", "CS 4"))
    else:
        clones = {clone.get("ID") for clone in manifest.get("PATHWAY-CLONES", [])}
        if len(set(priority)) != len(priority):
            out.append(Finding(122, "fail", "PATHWAY-PRIORITY repeats a pathway", "CS 4"))
        if not any(item in pathways for item in priority):
            out.append(Finding(123, "warn", "PATHWAY-PRIORITY names no pathway of the "
                               "playlist", "CS 5"))
        unknown = [item for item in priority if item not in pathways | clones]
        if unknown:
            out.append(Finding(124, "warn", f"PATHWAY-PRIORITY names pathways that neither "
                               f"the playlist nor a clone defines: {unknown}", "CS 4"))
    for clone in manifest.get("PATHWAY-CLONES", []):
        if not clone.get("BASE-ID") or not clone.get("ID") or \
                not isinstance(clone.get("URI-REPLACEMENT"), dict):
            out.append(Finding(125, "fail", f"a pathway clone lacks BASE-ID, ID or "
                               f"URI-REPLACEMENT: {clone}", "CS 5"))
    return out


def summarise(findings: list[Finding]) -> dict:
    """Findings grouped by rule, with a count."""
    grouped = {}
    for finding in findings:
        key = (finding["rule"], finding["level"])
        item = grouped.setdefault(key, {"rule": finding["rule"], "level": finding["level"],
                                        "reference": finding["reference"],
                                        "text": finding["text"], "count": 0})
        item["count"] += 1
    return {"fail": sorted((item for item in grouped.values() if item["level"] == "fail"),
                           key=lambda item: item["rule"]),
            "warn": sorted((item for item in grouped.values() if item["level"] == "warn"),
                           key=lambda item: item["rule"])}


def lab_fetch(node: str):
    """A `fetch` for `check_http` that asks a node of the edge lab."""
    import edge_lab as lab

    def fetch(path, headers=None, timeout=10):
        started = time.time()
        status, head, body = lab.curl(node, path, headers=headers, timeout=timeout)
        seconds = time.time() - started
        parsed = {}
        for line in head.splitlines()[1:]:
            name, _, value = line.partition(":")
            parsed[name.strip().lower()] = value.strip()
        return status, parsed, body, seconds
    return fetch


def watch(fetch, path: str, seconds: float, video: bool, fmp4: bool = True) -> dict:
    """Reload a playlist for a while and collect every finding."""
    findings = []
    previous = None
    reloads = 0
    gaps = []
    last_change = None
    last_head = None
    deadline = time.time() + seconds
    while time.time() < deadline:
        status, _headers, body, _ = fetch(path)
        now = time.time()
        if status != 200:
            findings.append(Finding(0, "fail", f"the playlist answered {status}", ""))
            time.sleep(0.2)
            continue
        playlist = parse_media(body)
        reloads += 1
        findings += check_media(playlist, video=video, fmp4=fmp4)
        if previous is not None:
            findings += check_reload(previous, playlist)
        head = last_position(playlist)
        if head != last_head:
            if last_change is not None:
                gaps.append(now - last_change)
            last_change = now
            last_head = head
        previous = playlist
        time.sleep(0.1)
    return {"reloads": reloads, "longest_time_without_a_new_part_s":
            round(max(gaps), 3) if gaps else None, **summarise(findings)}


def main() -> None:
    if len(sys.argv) < 4 or sys.argv[1] != "lab":
        sys.exit(__doc__)
    node, stream = sys.argv[2], int(sys.argv[3])
    seconds = float(sys.argv[sys.argv.index("--seconds") + 1]) if "--seconds" in sys.argv else 10
    fetch = lab_fetch(node)
    path = f"/live/{stream}/stream.m3u8"
    video = stream == 1
    result = watch(fetch, path, seconds, video)
    http = summarise(check_http(fetch, path, video=video))
    status, _, master, _ = fetch(f"/live/{stream}/master.m3u8?sid=spec-check")
    multivariant = summarise(check_multivariant(master)) if status == 200 else {
        "fail": [{"rule": 100, "text": f"the Multivariant Playlist answered {status}",
                  "count": 1}], "warn": []}
    print(json.dumps({"playlist": result, "http": http, "multivariant": multivariant},
                     indent=1))


if __name__ == "__main__":
    main()
