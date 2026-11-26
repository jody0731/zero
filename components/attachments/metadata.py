"""Validated header probing for attached files at ingest.

Reads only bounded header and trailer slices (never whole files) to sniff a
file's media type and extract verified image dimensions, audio/video durations,
or uncompressed PDF page counts. Any malformed, truncated, inconsistent, or
out-of-range input yields an empty dictionary rather than a guess, because
downstream callers treat these numbers as upper bounds.
"""

import io
import math
import os
import pathlib
import re
import stat
import struct

SVG_MEDIA_TYPE = "image/svg+xml"
SVG_HEADER_BYTES = 65_536

MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".svg": SVG_MEDIA_TYPE,
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp4": "video/mp4",
    ".m4a": "audio/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".pdf": "application/pdf",
}

MAX_DIMENSION = 1_000_000
MAX_DURATION_SECONDS = 1e7
MAX_PAGES = 100_000

MAX_JPEG_MARKERS = 4096
MAX_JPEG_SCAN_BYTES = 4 * 1024 * 1024
MAX_WAV_CHUNKS = 4096
MAX_MP4_BOXES = 4096
MAX_OGG_SCAN_BYTES = 128 * 1024
MAX_OGG_PAGES = 4096
MAX_MP3_SYNC_SCAN_BYTES = 64 * 1024
MAX_MP3_CBR_CHECK_FRAMES = 16
MAX_PDF_READ_BYTES = 2 * 1024 * 1024
MAX_PDF_TAIL_BYTES = 64 * 1024
MAX_PDF_XREF_SECTIONS = 32
MAX_PDF_XREF_ENTRIES = 100_000
MAX_PDF_TREE_DEPTH = 64
MAX_PDF_PARSE_DEPTH = 32
MAX_PDF_DICT_ITEMS = 2048

MP4_VIDEO_BRANDS = frozenset({
    b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6",
    b"mp41", b"mp42", b"avc1", b"M4V ", b"dash", b"msdh",
    b"msix", b"3gp4", b"3gp5", b"3gp6", b"3g2a",
})
MP4_AUDIO_BRANDS = frozenset({b"M4A ", b"M4B "})

JPEG_SOF_MARKERS = frozenset({
    0xC0, 0xC1, 0xC2, 0xC3,
    0xC5, 0xC6, 0xC7,
    0xC9, 0xCA, 0xCB,
    0xCD, 0xCE, 0xCF,
})
JPEG_STANDALONE_MARKERS = frozenset({
    0x01, 0xD0, 0xD1, 0xD2, 0xD3, 0xD4, 0xD5, 0xD6, 0xD7, 0xD8,
})

MP3_BITRATES_V1 = (
    0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0,
)
MP3_BITRATES_V2 = (
    0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0,
)
MP3_SAMPLE_RATES = {
    3: (44100, 48000, 32000),
    2: (22050, 24000, 16000),
    0: (11025, 12000, 8000),
}

OPUS_GRANULE_RATE = 48000

WAV_EXTENSIBLE_PCM_GUID = (
    b"\x01\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
)
WAV_EXTENSIBLE_FLOAT_GUID = (
    b"\x03\x00\x00\x00\x00\x00\x10\x00\x80\x00\x00\xaa\x00\x38\x9b\x71"
)

PDF_WHITESPACE = frozenset(b"\x00\t\n\x0c\r ")
PDF_DELIMITERS = frozenset(b"()<>[]{}/%")
PDF_XREF_ENTRY_RE = re.compile(rb"^(\d{10})\s+(\d{5})\s+([nf])$")
PDF_STARTXREF_RE = re.compile(rb"startxref[\r\n\s]+(\d+)[\r\n\s]+%%EOF")


def _doctype_end(text, start):
    brackets = 0
    while start < len(text):
        char = text[start]
        if char in "\"'":
            marker, start = char, start + 1
        elif text.startswith("<!--", start):
            marker, start = "-->", start + 4
        elif text.startswith("<?", start):
            marker, start = "?>", start + 2
        else:
            if char == "[":
                brackets += 1
            elif char == "]" and brackets:
                brackets -= 1
            elif char == ">" and brackets == 0:
                return start + 1
            start += 1
            continue
        end = text.find(marker, start)
        if end == -1:
            break
        start = end + len(marker)
    return len(text)


def _has_svg_header(head):
    at_limit = len(head) >= SVG_HEADER_BYTES
    head = head[:SVG_HEADER_BYTES]
    encoding = "utf-16" if head.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    text = head.decode(encoding, "replace")
    index = 0
    while index < len(text):
        if text[index].isspace():
            index += 1
            continue
        if text.startswith("<?", index):
            marker, start = "?>", index + 2
        elif text.startswith("<!--", index):
            marker, start = "-->", index + 4
        elif text[index:index + 9].upper() == "<!DOCTYPE":
            index = _doctype_end(text, index + 9)
            continue
        else:
            if text[index] != "<":
                return False
            end = index + 1
            while end < len(text) and not (
                text[end].isspace() or text[end] in "/>"
            ):
                end += 1
            if end == len(text):
                break
            prefix, colon, name = text[index + 1:end].rpartition(":")
            return name.lower() == "svg" and (
                not colon or (bool(prefix) and ":" not in prefix)
            )
        end = text.find(marker, start)
        if end == -1:
            break
        index = end + len(marker)
    # An unresolved prefix may hide an SVG root beyond the scan limit.
    return at_limit


def is_svg(name, head=b""):
    """Treat SVG names, SVG roots, and unresolved full prefixes as SVG."""
    if isinstance(name, str) and name.lower().endswith(".svg"):
        return True
    return _has_svg_header(head)


def media_type(name: str, head: bytes) -> str:
    """Return the file's media type, including the SVG download boundary."""
    raw_head = bytes(head) if head else b""
    if is_svg(name, raw_head):
        return SVG_MEDIA_TYPE
    sniffed = _sniff_magic(raw_head)
    if sniffed is not None:
        return sniffed
    if isinstance(name, str) and name:
        _, suffix = os.path.splitext(name)
        by_suffix = MEDIA_TYPES.get(suffix.lower())
        if by_suffix is not None:
            return by_suffix
    return "application/octet-stream"


def probe_bytes(data: bytes, media_type: str) -> dict:
    """Probe verified header metadata from in-memory bytes, never raising."""
    try:
        if not isinstance(media_type, str) or not data:
            return {}
        normalized = media_type.split(";", 1)[0].strip().lower()
        prober = _PROBERS.get(normalized)
        if prober is None:
            return {}
        result = prober(io.BytesIO(data), len(data))
        return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def probe(path: pathlib.Path, media_type: str) -> dict:
    """Probe verified header metadata for an attached file, never raising."""
    try:
        if not isinstance(media_type, str):
            return {}
        normalized = media_type.split(";", 1)[0].strip().lower()
        prober = _PROBERS.get(normalized)
        if prober is None:
            return {}
        file_path = pathlib.Path(path)
        st = file_path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_size <= 0:
            return {}
        with file_path.open("rb") as stream:
            open_st = os.fstat(stream.fileno())
            if not stat.S_ISREG(open_st.st_mode) or open_st.st_size <= 0:
                return {}
            result = prober(stream, open_st.st_size)
            return result if isinstance(result, dict) else {}
    except Exception:
        return {}


def _sniff_magic(head: bytes) -> str | None:
    if len(head) >= 8 and head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(head) >= 3 and head[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(head) >= 6 and head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(head) >= 12 and head[:4] == b"RIFF":
        form = head[8:12]
        if form == b"WEBP":
            return "image/webp"
        if form == b"WAVE":
            return "audio/wav"
    if len(head) >= 5 and head[:5] == b"%PDF-":
        return "application/pdf"
    if len(head) >= 5 and head[:4] == b"OggS" and head[4] == 0:
        return "audio/ogg"
    ftyp_media = _sniff_ftyp(head)
    if ftyp_media is not None:
        return ftyp_media
    if _has_id3v2_header(head):
        return "audio/mpeg"
    if len(head) >= 4 and _parse_mp3_frame_header(head[:4]) is not None:
        return "audio/mpeg"
    return None


def _sniff_ftyp(head: bytes) -> str | None:
    if len(head) < 12 or head[4:8] != b"ftyp":
        return None
    box_size = struct.unpack(">I", head[:4])[0]
    if box_size != 0 and box_size < 12:
        return None
    major_brand = head[8:12]
    if major_brand == b"qt  ":
        return "video/quicktime"
    if major_brand in MP4_AUDIO_BRANDS:
        return "audio/mp4"
    if major_brand in MP4_VIDEO_BRANDS:
        limit = len(head) if box_size == 0 else min(len(head), box_size)
        compat = [head[i : i + 4] for i in range(16, limit - 3, 4)]
        has_audio_brand = any(brand in MP4_AUDIO_BRANDS for brand in compat)
        has_explicit_video = any(
            brand in (b"mp41", b"mp42", b"avc1", b"M4V ") for brand in compat
        )
        if (
            major_brand in (b"isom", b"iso2")
            and has_audio_brand
            and not has_explicit_video
        ):
            return "audio/mp4"
        return "video/mp4"
    return None


def _has_id3v2_header(head: bytes) -> bool:
    if len(head) < 10 or head[:3] != b"ID3":
        return False
    if head[3] >= 0xFF or head[4] >= 0xFF:
        return False
    return all(byte < 0x80 for byte in head[6:10])


def _valid_dimensions(width: int, height: int) -> dict:
    if 1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION:
        return {"width": int(width), "height": int(height)}
    return {}


def _valid_duration(seconds: float) -> dict:
    if not math.isfinite(seconds):
        return {}
    if seconds <= 0 or seconds > MAX_DURATION_SECONDS:
        return {}
    return {"duration_seconds": round(float(seconds), 3)}


def _probe_png(stream, file_size: int) -> dict:
    head = stream.read(24)
    if len(head) < 24 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return {}
    chunk_length, chunk_type, width, height = struct.unpack(">I4sII", head[8:24])
    if chunk_length != 13 or chunk_type != b"IHDR":
        return {}
    return _valid_dimensions(width, height)


def _probe_gif(stream, file_size: int) -> dict:
    head = stream.read(10)
    if len(head) < 10 or head[:6] not in (b"GIF87a", b"GIF89a"):
        return {}
    width, height = struct.unpack("<HH", head[6:10])
    return _valid_dimensions(width, height)


def _probe_webp(stream, file_size: int) -> dict:
    head = stream.read(30)
    if len(head) < 25 or head[:4] != b"RIFF" or head[8:12] != b"WEBP":
        return {}
    chunk_id = head[12:16]
    chunk_size = struct.unpack("<I", head[16:20])[0]
    if chunk_id == b"VP8 ":
        if len(head) < 30 or chunk_size < 10:
            return {}
        # Only VP8 keyframes (bit 0 == 0) carry width and height.
        if (head[20] & 0x01) != 0 or head[23:26] != b"\x9d\x01\x2a":
            return {}
        raw_width, raw_height = struct.unpack("<HH", head[26:30])
        return _valid_dimensions(raw_width & 0x3FFF, raw_height & 0x3FFF)
    if chunk_id == b"VP8L":
        if chunk_size < 5 or head[20] != 0x2F:
            return {}
        bits = struct.unpack("<I", head[21:25])[0]
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return _valid_dimensions(width, height)
    if chunk_id == b"VP8X":
        if len(head) < 30 or chunk_size < 10:
            return {}
        if head[21:24] != b"\x00\x00\x00":
            return {}
        width = int.from_bytes(head[24:27], "little") + 1
        height = int.from_bytes(head[27:30], "little") + 1
        return _valid_dimensions(width, height)
    return {}


def _probe_jpeg(stream, file_size: int) -> dict:
    soi = stream.read(2)
    if soi != b"\xff\xd8":
        return {}
    scan_limit = min(file_size, MAX_JPEG_SCAN_BYTES)
    for _ in range(MAX_JPEG_MARKERS):
        if stream.tell() >= scan_limit:
            return {}
        prefix = stream.read(1)
        if prefix != b"\xff":
            return {}
        marker_byte = _read_jpeg_marker(stream)
        if marker_byte is None or marker_byte == 0x00:
            return {}
        if marker_byte in JPEG_STANDALONE_MARKERS:
            continue
        # Start of Scan (0xDA) and End of Image (0xD9) end header segments.
        if marker_byte in (0xD9, 0xDA):
            return {}
        length_bytes = stream.read(2)
        if len(length_bytes) < 2:
            return {}
        segment_length = struct.unpack(">H", length_bytes)[0]
        if segment_length < 2:
            return {}
        payload_length = segment_length - 2
        if stream.tell() + payload_length > file_size:
            return {}
        if marker_byte in JPEG_SOF_MARKERS:
            if payload_length < 5:
                return {}
            payload = stream.read(payload_length)
            if len(payload) != payload_length:
                return {}
            precision, height, width = struct.unpack(">BHH", payload[:5])
            if precision == 0:
                return {}
            return _valid_dimensions(width, height)
        next_pos = stream.tell() + payload_length
        if next_pos > scan_limit:
            return {}
        stream.seek(next_pos)
    return {}


def _read_jpeg_marker(stream) -> int | None:
    for _ in range(256):
        data = stream.read(1)
        if not data:
            return None
        value = data[0]
        if value != 0xFF:
            return value
    return None


def _probe_wav(stream, file_size: int) -> dict:
    header = stream.read(12)
    if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
        return {}
    byte_rate = None
    data_size = None
    offset = 12
    for _ in range(MAX_WAV_CHUNKS):
        if offset + 8 > file_size:
            break
        stream.seek(offset)
        chunk_header = stream.read(8)
        if len(chunk_header) < 8:
            return {}
        chunk_id = chunk_header[:4]
        chunk_size = struct.unpack("<I", chunk_header[4:8])[0]
        payload_start = offset + 8
        payload_end = payload_start + chunk_size
        if payload_end > file_size:
            return {}
        if chunk_id == b"fmt ":
            if byte_rate is not None:
                return {}
            byte_rate = _parse_wav_fmt(stream, chunk_size)
            if byte_rate is None:
                return {}
        elif chunk_id == b"data":
            if data_size is not None:
                return {}
            data_size = chunk_size
        if byte_rate is not None and data_size is not None:
            return _valid_duration(data_size / byte_rate)
        # RIFF chunks are word-aligned with one pad byte after odd sizes.
        offset = payload_end + (chunk_size & 1)
    return {}


def _parse_wav_fmt(stream, chunk_size: int) -> int | None:
    if chunk_size < 16:
        return None
    read_len = min(chunk_size, 40)
    fmt_bytes = stream.read(read_len)
    if len(fmt_bytes) < 16:
        return None
    (
        audio_format,
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
    ) = struct.unpack("<HHIIHH", fmt_bytes[:16])
    if channels < 1 or sample_rate < 1 or byte_rate < 1:
        return None
    if audio_format in (1, 3):
        if not _wav_pcm_consistent(
            channels, sample_rate, byte_rate, block_align, bits_per_sample
        ):
            return None
    elif audio_format == 0xFFFE:
        if chunk_size < 40 or len(fmt_bytes) < 40:
            return None
        subformat = fmt_bytes[24:40]
        if subformat in (WAV_EXTENSIBLE_PCM_GUID, WAV_EXTENSIBLE_FLOAT_GUID):
            if not _wav_pcm_consistent(
                channels, sample_rate, byte_rate, block_align, bits_per_sample
            ):
                return None
    return byte_rate


def _wav_pcm_consistent(
    channels: int,
    sample_rate: int,
    byte_rate: int,
    block_align: int,
    bits_per_sample: int,
) -> bool:
    if bits_per_sample < 8 or (bits_per_sample % 8) != 0:
        return False
    expected_align = channels * (bits_per_sample // 8)
    expected_rate = sample_rate * expected_align
    return block_align == expected_align and byte_rate == expected_rate


def _parse_mp3_frame_header(header: bytes) -> tuple | None:
    if len(header) < 4:
        return None
    b0, b1, b2, b3 = header[0], header[1], header[2], header[3]
    if b0 != 0xFF or (b1 & 0xE0) != 0xE0:
        return None
    version_id = (b1 >> 3) & 0x03
    layer_id = (b1 >> 1) & 0x03
    if version_id == 1 or layer_id != 1:
        return None
    bitrate_idx = (b2 >> 4) & 0x0F
    sr_idx = (b2 >> 2) & 0x03
    if bitrate_idx == 0 or bitrate_idx == 15 or sr_idx == 3:
        return None
    padding = (b2 >> 1) & 0x01
    channel_mode = (b3 >> 6) & 0x03
    if version_id == 3:
        bitrate_kbps = MP3_BITRATES_V1[bitrate_idx]
        samples_per_frame = 1152
        slot_factor = 144000
    else:
        bitrate_kbps = MP3_BITRATES_V2[bitrate_idx]
        samples_per_frame = 576
        slot_factor = 72000
    sample_rate = MP3_SAMPLE_RATES[version_id][sr_idx]
    frame_length = (slot_factor * bitrate_kbps) // sample_rate + padding
    if frame_length < 4:
        return None
    return (
        version_id,
        bitrate_kbps,
        sample_rate,
        samples_per_frame,
        frame_length,
        channel_mode,
    )


def _probe_mp3(stream, file_size: int) -> dict:
    audio_start = _skip_id3v2_tags(stream, file_size)
    if audio_start is None or audio_start >= file_size:
        return {}
    audio_end = _trim_mp3_trailing_tags(stream, file_size, audio_start)
    if audio_end <= audio_start:
        return {}
    found = _find_first_mp3_frame(stream, audio_start, audio_end)
    if found is None:
        return {}
    frame_pos, frame_info, skipped_nonzero = found
    (
        version_id,
        bitrate_kbps,
        sample_rate,
        samples_per_frame,
        frame_length,
        channel_mode,
    ) = frame_info
    if frame_pos + frame_length > audio_end:
        return {}
    stream.seek(frame_pos)
    first_frame = stream.read(frame_length)
    if len(first_frame) != frame_length:
        return {}
    vbr_result = _parse_mp3_vbr_header(
        first_frame, version_id, channel_mode, samples_per_frame, sample_rate
    )
    if vbr_result is not None:
        return vbr_result
    # Require a second valid frame if non-zero bytes preceded the first sync.
    if skipped_nonzero and frame_pos + frame_length == audio_end:
        return {}
    if not _verify_mp3_cbr_frames(
        stream,
        frame_pos + frame_length,
        audio_end,
        version_id,
        bitrate_kbps,
        sample_rate,
    ):
        return {}
    audio_bytes = audio_end - frame_pos
    return _valid_duration((audio_bytes * 8.0) / (bitrate_kbps * 1000.0))


def _skip_id3v2_tags(stream, file_size: int) -> int | None:
    offset = 0
    for _ in range(4):
        if offset + 10 > file_size:
            return offset
        stream.seek(offset)
        head = stream.read(10)
        if not _has_id3v2_header(head):
            return offset
        tag_size = (
            (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
        )
        footer_size = 10 if (head[5] & 0x10) else 0
        offset += 10 + tag_size + footer_size
        if offset > file_size:
            return None
    return offset


def _trim_mp3_trailing_tags(stream, file_size: int, audio_start: int) -> int:
    audio_end = file_size
    if audio_end - audio_start >= 128:
        stream.seek(audio_end - 128)
        if stream.read(3) == b"TAG":
            audio_end -= 128
    if audio_end - audio_start >= 32:
        stream.seek(audio_end - 32)
        ape_footer = stream.read(32)
        if ape_footer[:8] == b"APETAGEX":
            ape_size = struct.unpack("<I", ape_footer[12:16])[0]
            ape_flags = struct.unpack("<I", ape_footer[20:24])[0]
            # Bit 31 indicates the APEv2 tag also carries a 32-byte header.
            header_extra = 32 if (ape_flags & 0x80000000) else 0
            total_ape = ape_size + header_extra
            if 32 <= total_ape <= audio_end - audio_start:
                audio_end -= total_ape
    return audio_end


def _find_first_mp3_frame(stream, audio_start: int, audio_end: int):
    scan_len = min(audio_end - audio_start, MAX_MP3_SYNC_SCAN_BYTES)
    stream.seek(audio_start)
    buf = stream.read(scan_len)
    pos = 0
    while pos + 4 <= len(buf):
        idx = buf.find(b"\xff", pos)
        if idx < 0 or idx + 4 > len(buf):
            return None
        parsed = _parse_mp3_frame_header(buf[idx : idx + 4])
        if parsed is not None:
            skipped_nonzero = any(byte != 0 for byte in buf[:idx])
            return (audio_start + idx, parsed, skipped_nonzero)
        pos = idx + 1
    return None


def _parse_mp3_vbr_header(
    frame: bytes,
    version_id: int,
    channel_mode: int,
    samples_per_frame: int,
    sample_rate: int,
) -> dict | None:
    if version_id == 3:
        side_info_size = 17 if channel_mode == 3 else 32
    else:
        side_info_size = 9 if channel_mode == 3 else 17
    xing_offset = 4 + side_info_size
    if xing_offset + 8 <= len(frame):
        tag = frame[xing_offset : xing_offset + 4]
        if tag in (b"Xing", b"Info"):
            flags = struct.unpack(">I", frame[xing_offset + 4 : xing_offset + 8])[0]
            if flags & 0x01:
                if xing_offset + 12 > len(frame):
                    return {}
                frame_count = struct.unpack(
                    ">I", frame[xing_offset + 8 : xing_offset + 12]
                )[0]
                if frame_count == 0:
                    return {}
                return _valid_duration((frame_count * samples_per_frame) / sample_rate)
            # A Xing header without a frame count marks VBR of unknown length.
            if tag == b"Xing":
                return {}
    if len(frame) >= 54 and frame[36:40] == b"VBRI":
        frame_count = struct.unpack(">I", frame[50:54])[0]
        if frame_count == 0:
            return {}
        return _valid_duration((frame_count * samples_per_frame) / sample_rate)
    return None


def _verify_mp3_cbr_frames(
    stream,
    pos: int,
    audio_end: int,
    expected_version: int,
    expected_bitrate: int,
    expected_rate: int,
) -> bool:
    for _ in range(MAX_MP3_CBR_CHECK_FRAMES):
        if pos == audio_end:
            return True
        if pos + 4 > audio_end:
            return False
        stream.seek(pos)
        header = stream.read(4)
        parsed = _parse_mp3_frame_header(header)
        if parsed is None:
            return False
        version_id, bitrate_kbps, sample_rate, _, frame_length, _ = parsed
        if (
            version_id != expected_version
            or bitrate_kbps != expected_bitrate
            or sample_rate != expected_rate
        ):
            return False
        if pos + frame_length > audio_end:
            return False
        pos += frame_length
    return True


def _parse_ogg_page(buf: bytes, pos: int) -> tuple | None:
    if pos + 27 > len(buf) or buf[pos : pos + 4] != b"OggS":
        return None
    version = buf[pos + 4]
    header_type = buf[pos + 5]
    if version != 0 or (header_type & ~0x07) != 0:
        return None
    granule, serial = struct.unpack("<qI", buf[pos + 6 : pos + 18])
    segment_count = buf[pos + 26]
    payload_start = pos + 27 + segment_count
    if payload_start > len(buf):
        return None
    payload_len = sum(buf[pos + 27 : payload_start])
    page_end = payload_start + payload_len
    if page_end > len(buf):
        return None
    return (header_type, granule, serial, payload_start, page_end)


def _parse_ogg_codec_header(payload: bytes) -> tuple[int, int] | None:
    if len(payload) >= 30 and payload[:7] == b"\x01vorbis":
        vorbis_version = struct.unpack("<I", payload[7:11])[0]
        channels = payload[11]
        sample_rate = struct.unpack("<I", payload[12:16])[0]
        if vorbis_version != 0 or channels < 1 or sample_rate < 1:
            return None
        return (sample_rate, 0)
    if len(payload) >= 19 and payload[:8] == b"OpusHead":
        opus_version = payload[8]
        channels = payload[9]
        pre_skip = struct.unpack("<H", payload[10:12])[0]
        if (opus_version & 0xF0) != 0 or channels < 1:
            return None
        return (OPUS_GRANULE_RATE, pre_skip)
    return None


def _probe_ogg(stream, file_size: int) -> dict:
    if file_size <= MAX_OGG_SCAN_BYTES:
        buf = stream.read(file_size)
        if len(buf) != file_size:
            return {}
        return _probe_ogg_full_buffer(buf)
    head = stream.read(65536)
    first_page = _parse_ogg_page(head, 0)
    if first_page is None:
        return {}
    header_type, _, bos_serial, payload_start, _ = first_page
    if (header_type & 0x02) == 0:
        return {}
    codec = _parse_ogg_codec_header(head[payload_start:first_page[4]])
    if codec is None:
        return {}
    sample_rate, pre_skip = codec
    tail_size = min(file_size, MAX_OGG_SCAN_BYTES)
    stream.seek(file_size - tail_size)
    tail = stream.read(tail_size)
    if len(tail) != tail_size:
        return {}
    last_granule = _find_last_ogg_granule_in_tail(tail, bos_serial)
    if last_granule is None:
        return {}
    effective = last_granule - pre_skip
    if effective <= 0:
        return {}
    return _valid_duration(effective / sample_rate)


def _probe_ogg_full_buffer(buf: bytes) -> dict:
    first_page = _parse_ogg_page(buf, 0)
    if first_page is None:
        return {}
    header_type, granule, bos_serial, payload_start, pos = first_page
    if (header_type & 0x02) == 0:
        return {}
    codec = _parse_ogg_codec_header(buf[payload_start:pos])
    if codec is None:
        return {}
    sample_rate, pre_skip = codec
    last_granule = granule if granule >= 0 else None
    bos_ended = bool(header_type & 0x04)
    for _ in range(MAX_OGG_PAGES):
        if pos == len(buf):
            break
        page = _parse_ogg_page(buf, pos)
        if page is None:
            return {}
        page_type, page_granule, serial, _, next_pos = page
        if serial == bos_serial:
            if (page_type & 0x02) != 0:
                return {}
            if page_granule >= 0:
                last_granule = page_granule
            if (page_type & 0x04) != 0:
                bos_ended = True
        elif bos_ended and (page_type & 0x02) != 0:
            # Chained logical streams reset granules, so tail granule is not total.
            return {}
        pos = next_pos
    else:
        if pos != len(buf):
            return {}
    if last_granule is None:
        return {}
    effective = last_granule - pre_skip
    if effective <= 0:
        return {}
    return _valid_duration(effective / sample_rate)


def _find_last_ogg_granule_in_tail(tail: bytes, bos_serial: int) -> int | None:
    search_end = len(tail)
    for _ in range(2048):
        idx = tail.rfind(b"OggS", 0, search_end)
        if idx < 0:
            return None
        search_end = idx
        page = _parse_ogg_page(tail, idx)
        if page is None:
            continue
        if not _ogg_pages_reach_eof(tail, idx):
            continue
        page_type, granule, serial, _, _ = page
        # A BOS page inside the tail indicates a chained Ogg stream.
        if (page_type & 0x02) != 0:
            return None
        if serial == bos_serial and granule >= 0:
            return granule
    return None


def _ogg_pages_reach_eof(tail: bytes, start: int) -> bool:
    pos = start
    for _ in range(512):
        if pos == len(tail):
            return True
        page = _parse_ogg_page(tail, pos)
        if page is None:
            return False
        pos = page[4]
    return False


def _probe_mp4(stream, file_size: int) -> dict:
    box_budget = [MAX_MP4_BOXES]
    moov_range = _find_mp4_box(stream, 0, file_size, b"moov", box_budget)
    if moov_range is None:
        return {}
    moov_start, moov_end = moov_range
    mvhd_range = _find_unique_mp4_box(stream, moov_start, moov_end, b"mvhd", box_budget)
    if mvhd_range is None:
        return {}
    mvhd_start, mvhd_end = mvhd_range
    payload_len = mvhd_end - mvhd_start
    if payload_len < 20:
        return {}
    stream.seek(mvhd_start)
    payload = stream.read(min(payload_len, 32))
    if len(payload) < 20:
        return {}
    version = payload[0]
    if version == 0:
        timescale, duration = struct.unpack(">II", payload[12:20])
        if timescale == 0 or duration == 0 or duration == 0xFFFFFFFF:
            return {}
        return _valid_duration(duration / timescale)
    if version == 1:
        if len(payload) < 32:
            return {}
        timescale, duration = struct.unpack(">IQ", payload[20:32])
        if timescale == 0 or duration == 0 or duration == 0xFFFFFFFFFFFFFFFF:
            return {}
        return _valid_duration(duration / timescale)
    return {}


def _read_mp4_box_header(stream, offset: int, parent_end: int) -> tuple | None:
    if offset + 8 > parent_end:
        return None
    stream.seek(offset)
    header = stream.read(8)
    if len(header) < 8:
        return None
    size, box_type = struct.unpack(">I4s", header)
    if size == 0:
        box_end = parent_end
        payload_start = offset + 8
    elif size == 1:
        if offset + 16 > parent_end:
            return None
        ext_bytes = stream.read(8)
        if len(ext_bytes) < 8:
            return None
        ext_size = struct.unpack(">Q", ext_bytes)[0]
        if ext_size < 16 or offset + ext_size > parent_end:
            return None
        box_end = offset + ext_size
        payload_start = offset + 16
    elif size < 8:
        return None
    else:
        if offset + size > parent_end:
            return None
        box_end = offset + size
        payload_start = offset + 8
    return (box_type, payload_start, box_end)


def _find_mp4_box(
    stream, start: int, end: int, target: bytes, box_budget: list[int]
) -> tuple[int, int] | None:
    offset = start
    while offset < end:
        if box_budget[0] <= 0:
            return None
        box_budget[0] -= 1
        parsed = _read_mp4_box_header(stream, offset, end)
        if parsed is None:
            return None
        box_type, payload_start, box_end = parsed
        if box_type == target:
            return (payload_start, box_end)
        offset = box_end
    return None


def _find_unique_mp4_box(
    stream, start: int, end: int, target: bytes, box_budget: list[int]
) -> tuple[int, int] | None:
    offset = start
    found = None
    while offset < end:
        if box_budget[0] <= 0:
            return None
        box_budget[0] -= 1
        parsed = _read_mp4_box_header(stream, offset, end)
        if parsed is None:
            return None
        box_type, payload_start, box_end = parsed
        if box_type == target:
            if found is not None:
                return None
            found = (payload_start, box_end)
        offset = box_end
    return found


class _PdfAbort(Exception):
    """Raised internally to bail out of PDF validation into an empty dict."""


class _PdfRef:
    """Indirect PDF object reference (object number and generation)."""

    __slots__ = ("num", "gen")

    def __init__(self, num: int, gen: int):
        self.num = num
        self.gen = gen

    def key(self) -> tuple[int, int]:
        return (self.num, self.gen)

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, _PdfRef)
            and self.num == other.num
            and self.gen == other.gen
        )

    def __hash__(self) -> int:
        return hash((self.num, self.gen))


class _PdfReader:
    """Bounded random-access reader for uncompressed classic PDF validation."""

    def __init__(self, stream, file_size: int):
        self.stream = stream
        self.file_size = file_size
        self.remaining_budget = MAX_PDF_READ_BYTES
        self.full_data: bytes | None = None
        if file_size <= MAX_PDF_READ_BYTES:
            stream.seek(0)
            data = stream.read(file_size)
            if len(data) != file_size:
                raise _PdfAbort()
            self.full_data = data
            self.remaining_budget = 0

    def read_slice(self, offset: int, length: int) -> bytes:
        if offset < 0 or offset >= self.file_size or length <= 0:
            return b""
        clamped = min(length, self.file_size - offset)
        if self.full_data is not None:
            return self.full_data[offset : offset + clamped]
        if clamped > self.remaining_budget:
            raise _PdfAbort()
        self.remaining_budget -= clamped
        self.stream.seek(offset)
        data = self.stream.read(clamped)
        if len(data) != clamped:
            raise _PdfAbort()
        return data


def _probe_pdf(stream, file_size: int) -> dict:
    try:
        reader = _PdfReader(stream, file_size)
        head = reader.read_slice(0, 8)
        if len(head) < 5 or head[:5] != b"%PDF-":
            return {}
        startxref = _find_pdf_startxref(reader)
        xref, root_ref = _load_pdf_xref_chain(reader, startxref)
        if root_ref is None:
            return {}
        root_obj = _read_pdf_object(reader, xref, root_ref)
        if not isinstance(root_obj, dict):
            return {}
        root_type = root_obj.get(b"/Type")
        if root_type is not None and root_type != b"/Catalog":
            return {}
        pages_ref = root_obj.get(b"/Pages")
        if not isinstance(pages_ref, _PdfRef):
            return {}
        page_count = _validate_pdf_page_tree(reader, xref, pages_ref)
        if 1 <= page_count <= MAX_PAGES:
            return {"pages": page_count}
        return {}
    except _PdfAbort:
        return {}


def _find_pdf_startxref(reader: _PdfReader) -> int:
    tail_len = min(reader.file_size, MAX_PDF_TAIL_BYTES)
    tail = reader.read_slice(reader.file_size - tail_len, tail_len)
    matches = list(PDF_STARTXREF_RE.finditer(tail))
    if not matches:
        raise _PdfAbort()
    last_match = matches[-1]
    trailing = tail[last_match.end() :]
    if len(trailing) > 1024 or any(b not in PDF_WHITESPACE for b in trailing):
        raise _PdfAbort()
    offset = int(last_match.group(1))
    if offset <= 0 or offset >= reader.file_size:
        raise _PdfAbort()
    return offset


def _load_pdf_xref_chain(
    reader: _PdfReader, startxref: int
) -> tuple[dict[tuple[int, int], int], _PdfRef | None]:
    xref: dict[tuple[int, int], int] = {}
    freed: set[tuple[int, int]] = set()
    root_ref: _PdfRef | None = None
    visited_offsets: set[int] = set()
    total_entries = 0
    current = startxref

    for _ in range(MAX_PDF_XREF_SECTIONS):
        if current in visited_offsets or current <= 0 or current >= reader.file_size:
            raise _PdfAbort()
        visited_offsets.add(current)
        section_entries, trailer = _parse_pdf_xref_section(reader, current)
        if b"/Encrypt" in trailer or b"/XRefStm" in trailer:
            raise _PdfAbort()
        if root_ref is None:
            candidate = trailer.get(b"/Root")
            if isinstance(candidate, _PdfRef):
                root_ref = candidate
        for key, entry_offset in section_entries:
            total_entries += 1
            if total_entries > MAX_PDF_XREF_ENTRIES:
                raise _PdfAbort()
            if key in xref or key in freed:
                continue
            if entry_offset is None:
                freed.add(key)
            else:
                xref[key] = entry_offset
        prev = trailer.get(b"/Prev")
        if prev is None:
            break
        if not isinstance(prev, int) or isinstance(prev, bool):
            raise _PdfAbort()
        current = prev
    else:
        raise _PdfAbort()

    return xref, root_ref


def _parse_pdf_xref_section(
    reader: _PdfReader, offset: int
) -> tuple[list[tuple[tuple[int, int], int | None]], dict]:
    # Classic xref tables start with the literal "xref" keyword, not an object.
    prefix = reader.read_slice(offset, 16)
    if not prefix.startswith(b"xref"):
        raise _PdfAbort()
    if len(prefix) > 4 and prefix[4] not in PDF_WHITESPACE:
        raise _PdfAbort()
    buf = _read_pdf_xref_window(reader, offset)
    pos = _pdf_skip_ws(buf, 4)
    entries: list[tuple[tuple[int, int], int | None]] = []
    while True:
        pos = _pdf_skip_ws(buf, pos)
        if pos >= len(buf):
            raise _PdfAbort()
        if buf[pos : pos + 7] == b"trailer" and (
            pos + 7 == len(buf)
            or buf[pos + 7] in PDF_WHITESPACE
            or buf[pos + 7] in PDF_DELIMITERS
        ):
            pos += 7
            break
        line, pos = _pdf_read_line(buf, pos)
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            raise _PdfAbort()
        first_num = int(parts[0])
        count = int(parts[1])
        if count < 0 or len(entries) + count > MAX_PDF_XREF_ENTRIES:
            raise _PdfAbort()
        for idx in range(count):
            entry_line, pos = _pdf_read_line(buf, pos)
            match = PDF_XREF_ENTRY_RE.match(entry_line)
            if match is None:
                raise _PdfAbort()
            entry_offset = int(match.group(1))
            gen = int(match.group(2))
            flag = match.group(3)
            key = (first_num + idx, gen)
            if flag == b"n":
                if entry_offset <= 0 or entry_offset >= reader.file_size:
                    raise _PdfAbort()
                entries.append((key, entry_offset))
            else:
                entries.append((key, None))
    trailer, _ = _pdf_parse_value(buf, pos, 0)
    if not isinstance(trailer, dict):
        raise _PdfAbort()
    return entries, trailer


def _read_pdf_xref_window(reader: _PdfReader, offset: int) -> bytes:
    if reader.full_data is not None:
        return reader.full_data[offset:]
    window = 65536
    while True:
        chunk = reader.read_slice(offset, window)
        trailer_idx = chunk.find(b"trailer")
        if trailer_idx >= 0 and chunk.find(b">>", trailer_idx) >= 0:
            # Ensure the whole trailer dictionary is present before parsing.
            try:
                _pdf_parse_value(chunk, trailer_idx + 7, 0)
                return chunk
            except _PdfAbort:
                pass
        if offset + len(chunk) >= reader.file_size or window >= MAX_PDF_READ_BYTES:
            raise _PdfAbort()
        window = min(window * 2, MAX_PDF_READ_BYTES)


def _read_pdf_object(
    reader: _PdfReader, xref: dict[tuple[int, int], int], ref: _PdfRef
):
    offset = xref.get(ref.key())
    if offset is None:
        raise _PdfAbort()
    if reader.full_data is not None:
        return _parse_pdf_indirect_object(reader.full_data, offset, ref)
    window = 1024
    while True:
        chunk = reader.read_slice(offset, window)
        try:
            return _parse_pdf_indirect_object(chunk, 0, ref)
        except _PdfAbort:
            if offset + len(chunk) >= reader.file_size or window >= 262144:
                raise _PdfAbort()
            window = min(window * 2, 262144)


def _parse_pdf_indirect_object(buf: bytes, start: int, expected: _PdfRef):
    num_tok, pos = _pdf_next_token(buf, start)
    gen_tok, pos = _pdf_next_token(buf, pos)
    obj_tok, pos = _pdf_next_token(buf, pos)
    if (
        not isinstance(num_tok, int)
        or not isinstance(gen_tok, int)
        or obj_tok != b"obj"
    ):
        raise _PdfAbort()
    if num_tok != expected.num or gen_tok != expected.gen:
        raise _PdfAbort()
    value, pos = _pdf_parse_value(buf, pos, 0)
    end_tok, _ = _pdf_next_token(buf, pos)
    # Catalog, Pages, Page, Kids, and Count objects are never stream objects.
    if end_tok != b"endobj":
        raise _PdfAbort()
    return value


def _validate_pdf_page_tree(
    reader: _PdfReader,
    xref: dict[tuple[int, int], int],
    root_pages_ref: _PdfRef,
) -> int:
    visited: set[tuple[int, int]] = set()
    # Explicit stack frames: (ref, parent_ref, depth, declared_count, child_iter, acc)
    stack: list[list] = [[root_pages_ref, None, 0, None, None, 0]]
    nodes_seen = 0
    max_nodes = MAX_PAGES + 10_000
    root_total = 0

    while stack:
        frame = stack[-1]
        ref, parent_ref, depth, declared, child_iter, acc = frame
        if declared is None:
            if depth > MAX_PDF_TREE_DEPTH:
                raise _PdfAbort()
            nodes_seen += 1
            if nodes_seen > max_nodes:
                raise _PdfAbort()
            key = ref.key()
            if key in visited:
                raise _PdfAbort()
            visited.add(key)
            node = _read_pdf_object(reader, xref, ref)
            if not isinstance(node, dict):
                raise _PdfAbort()
            node_type = node.get(b"/Type")
            if node_type == b"/ObjStm":
                raise _PdfAbort()
            node_parent = node.get(b"/Parent")
            if parent_ref is None:
                if node_parent is not None:
                    raise _PdfAbort()
            elif node_parent is not None and node_parent != parent_ref:
                raise _PdfAbort()
            if node_type == b"/Page":
                if b"/Kids" in node:
                    raise _PdfAbort()
                stack.pop()
                if not stack:
                    raise _PdfAbort()
                stack[-1][5] += 1
                continue
            if node_type != b"/Pages":
                raise _PdfAbort()
            count_val = node.get(b"/Count")
            if isinstance(count_val, _PdfRef):
                count_val = _read_pdf_object(reader, xref, count_val)
            if (
                not isinstance(count_val, int)
                or isinstance(count_val, bool)
                or count_val < 0
                or count_val > MAX_PAGES
            ):
                raise _PdfAbort()
            if parent_ref is None and count_val == 0:
                raise _PdfAbort()
            kids_val = node.get(b"/Kids")
            if isinstance(kids_val, _PdfRef):
                kids_val = _read_pdf_object(reader, xref, kids_val)
            if not isinstance(kids_val, list):
                raise _PdfAbort()
            for kid in kids_val:
                if not isinstance(kid, _PdfRef):
                    raise _PdfAbort()
            frame[3] = count_val
            frame[4] = iter(kids_val)
            continue

        next_kid = next(child_iter, None)
        if next_kid is not None:
            stack.append([next_kid, ref, depth + 1, None, None, 0])
            continue
        if acc != declared:
            raise _PdfAbort()
        stack.pop()
        if stack:
            stack[-1][5] += acc
        else:
            root_total = acc

    return root_total


def _pdf_skip_ws(buf: bytes, pos: int) -> int:
    length = len(buf)
    while pos < length:
        byte = buf[pos]
        if byte in PDF_WHITESPACE:
            pos += 1
        elif byte == 0x25:
            # Skip "%" comment to end of line.
            pos += 1
            while pos < length and buf[pos] not in (0x0A, 0x0D):
                pos += 1
        else:
            break
    return pos


def _pdf_read_line(buf: bytes, pos: int) -> tuple[bytes, int]:
    pos = _pdf_skip_ws(buf, pos)
    if pos >= len(buf):
        raise _PdfAbort()
    end = pos
    length = len(buf)
    while end < length and buf[end] not in (0x0A, 0x0D):
        end += 1
    line = buf[pos:end].strip()
    if end < length and buf[end] == 0x0D:
        end += 1
        if end < length and buf[end] == 0x0A:
            end += 1
    elif end < length and buf[end] == 0x0A:
        end += 1
    return line, end


def _pdf_next_token(buf: bytes, pos: int):
    pos = _pdf_skip_ws(buf, pos)
    length = len(buf)
    if pos >= length:
        raise _PdfAbort()
    byte = buf[pos]
    if byte == 0x3C:
        if pos + 1 < length and buf[pos + 1] == 0x3C:
            return b"<<", pos + 2
        end = buf.find(b">", pos + 1)
        if end < 0:
            raise _PdfAbort()
        return buf[pos : end + 1], end + 1
    if byte == 0x3E:
        if pos + 1 < length and buf[pos + 1] == 0x3E:
            return b">>", pos + 2
        raise _PdfAbort()
    if byte in (0x5B, 0x5D):
        return buf[pos : pos + 1], pos + 1
    if byte == 0x28:
        end = _pdf_skip_literal_string(buf, pos)
        return buf[pos:end], end
    if byte == 0x2F:
        end = pos + 1
        while end < length and buf[end] not in PDF_WHITESPACE and (
            buf[end] not in PDF_DELIMITERS
        ):
            end += 1
        return _pdf_decode_name(buf[pos:end]), end
    end = pos
    while end < length and buf[end] not in PDF_WHITESPACE and (
        buf[end] not in PDF_DELIMITERS
    ):
        end += 1
    raw = buf[pos:end]
    if not raw:
        raise _PdfAbort()
    if raw == b"true":
        return True, end
    if raw == b"false":
        return False, end
    if raw == b"null":
        return None, end
    try:
        return int(raw), end
    except ValueError:
        pass
    try:
        return float(raw), end
    except ValueError:
        return raw, end


def _pdf_skip_literal_string(buf: bytes, pos: int) -> int:
    length = len(buf)
    depth = 0
    idx = pos
    while idx < length:
        byte = buf[idx]
        if byte == 0x5C:
            idx += 2
            continue
        if byte == 0x28:
            depth += 1
        elif byte == 0x29:
            depth -= 1
            if depth == 0:
                return idx + 1
        idx += 1
    raise _PdfAbort()


def _pdf_decode_name(raw: bytes) -> bytes:
    if b"#" not in raw:
        return raw
    out = bytearray()
    idx = 0
    length = len(raw)
    while idx < length:
        if raw[idx] == 0x23 and idx + 2 < length:
            try:
                out.append(int(raw[idx + 1 : idx + 3], 16))
                idx += 3
                continue
            except ValueError:
                pass
        out.append(raw[idx])
        idx += 1
    return bytes(out)


def _pdf_parse_value(buf: bytes, pos: int, depth: int):
    if depth > MAX_PDF_PARSE_DEPTH:
        raise _PdfAbort()
    token, next_pos = _pdf_next_token(buf, pos)
    if token == b"<<":
        result = {}
        cur = next_pos
        for _ in range(MAX_PDF_DICT_ITEMS):
            key, cur = _pdf_next_token(buf, cur)
            if key == b">>":
                return result, cur
            if not isinstance(key, bytes) or not key.startswith(b"/"):
                raise _PdfAbort()
            val, cur = _pdf_parse_value(buf, cur, depth + 1)
            result[key] = val
        raise _PdfAbort()
    if token == b"[":
        items = []
        cur = next_pos
        max_items = MAX_PAGES + 1024
        for _ in range(max_items):
            peek_pos = _pdf_skip_ws(buf, cur)
            if peek_pos >= len(buf):
                raise _PdfAbort()
            if buf[peek_pos] == 0x5D:
                return items, peek_pos + 1
            val, cur = _pdf_parse_value(buf, cur, depth + 1)
            items.append(val)
        raise _PdfAbort()
    if isinstance(token, int) and not isinstance(token, bool) and token >= 0:
        # Look ahead for an indirect object reference "N G R".
        try:
            gen_tok, after_gen = _pdf_next_token(buf, next_pos)
            if (
                isinstance(gen_tok, int)
                and not isinstance(gen_tok, bool)
                and gen_tok >= 0
            ):
                r_tok, after_r = _pdf_next_token(buf, after_gen)
                if r_tok == b"R":
                    return _PdfRef(token, gen_tok), after_r
        except _PdfAbort:
            pass
    return token, next_pos


_PROBERS = {
    "image/png": _probe_png,
    "image/jpeg": _probe_jpeg,
    "image/jpg": _probe_jpeg,
    "image/webp": _probe_webp,
    "image/gif": _probe_gif,
    "audio/wav": _probe_wav,
    "audio/x-wav": _probe_wav,
    "audio/wave": _probe_wav,
    "audio/mpeg": _probe_mp3,
    "audio/mp3": _probe_mp3,
    "audio/ogg": _probe_ogg,
    "audio/opus": _probe_ogg,
    "application/ogg": _probe_ogg,
    "video/mp4": _probe_mp4,
    "audio/mp4": _probe_mp4,
    "video/quicktime": _probe_mp4,
    "video/x-m4v": _probe_mp4,
    "audio/x-m4a": _probe_mp4,
    "application/pdf": _probe_pdf,
}
