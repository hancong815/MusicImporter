#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Music Importer v17 (macOS)
Batch music conversion, metadata/artwork staging, editable split preview, single-pass batch splitting, metadata/artwork staging, direct Apple Music import, and cancellable task control.
Original source files are never modified.
Dependencies: FFmpeg, Python 3, Mutagen (pip install mutagen)
"""

import os
import re
import subprocess
import threading
import tkinter as tk
import unicodedata
import logging
import platform
import sys
import shlex
import time
import json
import urllib.parse
import urllib.request
from tkinter import ttk, filedialog, messagebox
from pathlib import Path as P

SUPPORTED = {
    ".flac", ".wav", ".ape", ".aiff", ".aif", ".dsf", ".dff", ".wv",
    ".mp3", ".m4a", ".aac", ".ogg", ".opus"
}

TAG_FIELDS = [
    ("title", "歌曲名称 / Title"),
    ("artist", "歌手 / Artist"),
    ("album", "专辑 / Album"),
    ("albumartist", "专辑艺术家 / Album Artist"),
    ("date", "年份 / Date"),
    ("genre", "流派 / Genre"),
    ("tracknumber", "曲序 / Track Number"),
    ("discnumber", "碟号 / Disc Number"),
]

# Conservative title-cleanup patterns: remove only common, clearly promotional/file-format labels.
REMOVE_PATTERNS = [
    r"\[(?:FLAC|WAV|APE|MP3|AAC|ALAC|DSF|DFF|无损(?:音乐|音质)?|高音质|免费下载|音乐下载)\]",
    r"\((?:FLAC|WAV|APE|MP3|AAC|ALAC|DSF|DFF|无损(?:音乐|音质)?|高音质|免费下载|音乐下载)\)",
    r"\[(?:\d{2,3}\s*kbps|\d{1,2}\s*bit(?:\s*/\s*\d{2,3}\s*kHz)?)\]",
    r"\((?:\d{2,3}\s*kbps|\d{1,2}\s*bit(?:\s*/\s*\d{2,3}\s*kHz)?)\)",
    r"\[(?:Official\s*(?:Audio|MV|Video)|Lyrics?\s*Video)\]",
    r"\((?:Official\s*(?:Audio|MV|Video)|Lyrics?\s*Video)\)",
]
TRACK_PREFIX = re.compile(r"^\s*(?:(?:track\s*)?(\d{1,3})\s*[-._、)]\s*|(\d{1,3})\s+|(\d{1,3})(?=[\u4e00-\u9fffA-Za-z]))", re.I)


def setup_diagnostic_logger():
    """Create a persistent diagnostic log in the user's macOS Logs folder."""
    logger = logging.getLogger("MusicImporter")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger
    try:
        log_dir = P.home() / "Library" / "Logs" / "MusicImporter"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "musicimporter.log"
    except Exception:
        # Fallback is useful if the home Logs directory is unavailable.
        log_path = P(os.environ.get("TMPDIR", "/tmp")) / "musicimporter.log"
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.info("========== MusicImporter diagnostic session started ==========")
    logger.info("Log file: %s", log_path)
    logger.info("Python: %s | executable=%s", sys.version.replace("\n", " "), sys.executable)
    logger.info("Platform: %s | machine=%s | processor=%s", platform.platform(), platform.machine(), platform.processor())
    logger.info("Process architecture: %s | app bundle=%s", platform.architecture(), bool(getattr(sys, "frozen", False)))
    logger.info("Working directory: %s | argv=%s", os.getcwd(), sys.argv)
    logger.info("PATH=%s", os.environ.get("PATH", ""))
    return logger


DIAG_LOG = setup_diagnostic_logger()


def log_file_info(label, path):
    try:
        stat = P(path).stat()
        logger = logging.getLogger("MusicImporter")
        logger.info("%s: path=%s | size=%d bytes (%.2f MiB) | modified=%s", label, path, stat.st_size,
                    stat.st_size / (1024 * 1024), stat.st_mtime)
    except Exception:
        logging.getLogger("MusicImporter").info("%s: path=%s | file info unavailable", label, path)


def find_ffmpeg():
    candidates = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "ffmpeg"]
    logger = logging.getLogger("MusicImporter")
    for candidate in candidates:
        logger.info("Checking FFmpeg candidate: %s", candidate)
        if candidate == "ffmpeg":
            try:
                subprocess.run([candidate, "-version"], capture_output=True, text=True, check=True, timeout=10)
                resolved = subprocess.run(["which", candidate], capture_output=True, text=True, timeout=5).stdout.strip() or candidate
                logger.info("FFmpeg selected from PATH: %s (resolved=%s)", candidate, resolved)
                return candidate
            except Exception as exc:
                logger.info("FFmpeg candidate unavailable: %s (%s)", candidate, exc)
        elif os.path.isfile(candidate):
            logger.info("FFmpeg selected: %s", candidate)
            return candidate
    logger.error("No FFmpeg executable found")
    return None


def read_tags(path):
    """Read supported tags through Mutagen; return normalized field dictionary."""
    empty = {key: "" for key, _ in TAG_FIELDS}
    try:
        from mutagen import File
        audio = File(path, easy=True)
        if not audio or not audio.tags:
            empty["title"] = P(path).stem
            return empty

        def val(*keys):
            for key in keys:
                item = audio.tags.get(key)
                if item:
                    if isinstance(item, (list, tuple)):
                        return str(item[0]) if item else ""
                    return str(item)
            return ""

        empty.update({
            "title": val("title") or P(path).stem,
            "artist": val("artist"),
            "album": val("album"),
            "albumartist": val("albumartist", "album artist"),
            "date": val("date", "year"),
            "genre": val("genre"),
            "tracknumber": val("tracknumber", "track"),
            "discnumber": val("discnumber", "disc"),
        })
    except Exception:
        empty["title"] = P(path).stem
    return empty



def read_lyrics(path):
    """Read embedded lyrics from common audio tag formats without altering text."""
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(str(path))
        tags = getattr(audio, "tags", None) if audio else None
        if not tags:
            return ""
        # ID3 unsynchronized lyrics (MP3/AIFF/WAV) are stored in USLT frames.
        try:
            frames = tags.getall("USLT") if hasattr(tags, "getall") else []
            for frame in frames:
                if getattr(frame, "text", None):
                    return str(frame.text)
        except Exception:
            pass
        # MP4/M4A lyric atom, Vorbis comments, and common alternate lyric keys.
        for key in ("\u00a9lyr", "lyrics", "LYRICS", "UNSYNCEDLYRICS"):
            try:
                value = tags.get(key) if hasattr(tags, "get") else None
            except (KeyError, ValueError, TypeError):
                # Some Mutagen tag containers raise ValueError for keys that
                # are invalid for their container instead of returning None.
                continue
            if value:
                if isinstance(value, (list, tuple)):
                    value = value[0]
                return str(value)
        # Fallback through Mutagen's easy interface for other supported formats.
        easy = MutagenFile(str(path), easy=True)
        if easy and easy.tags:
            value = easy.tags.get("lyrics") or easy.tags.get("unsyncedlyrics")
            if value:
                return str(value[0] if isinstance(value, (list, tuple)) else value)
    except Exception:
        logging.getLogger("MusicImporter").exception("LYRICS READ FAILED | source=%s", path)
    return ""


def write_lyrics(path, lyrics):
    """Write lyrics to destination's native tag, retaining Unicode text."""
    if not lyrics:
        return False
    ext = P(path).suffix.lower()
    from mutagen import File as MutagenFile
    audio = MutagenFile(str(path))
    if audio is None:
        raise RuntimeError(f"无法识别歌词输出格式：{P(path).name}")
    tags = getattr(audio, "tags", None)
    if tags is None:
        try:
            audio.add_tags()
            tags = audio.tags
        except Exception as exc:
            raise RuntimeError(f"目标格式不支持写入歌词：{P(path).name}") from exc
    if ext in {".mp3", ".aiff", ".aif", ".wav"}:
        from mutagen.id3 import USLT
        if hasattr(tags, "delall"):
            tags.delall("USLT")
            tags.add(USLT(encoding=3, lang="eng", desc="", text=lyrics))
        else:
            raise RuntimeError(f"该文件无法写入 ID3 歌词：{P(path).name}")
    elif ext in {".m4a", ".aac"}:
        tags["\u00a9lyr"] = [lyrics]
    elif ext in {".flac", ".ogg", ".opus"}:
        tags["LYRICS"] = [lyrics]
    else:
        raise RuntimeError(f"暂不支持此输出格式的歌词写入：{ext}")
    audio.save()
    return True


def _lyric_garble_score(text):
    if not text:
        return 0.0
    suspicious = sum(text.count(ch) for ch in ("\ufffd", "Ã", "Â", "â€", "æ", "å", "ä", "ï¿"))
    # Replacement characters and common UTF-8-as-Latin-1/CP1252 artifacts are strong signals.
    return suspicious / max(1, len(text))


def recover_mojibake_lyrics(text):
    """Try reversible common encoding repairs; return (candidate, changed, confidence)."""
    if not text:
        return text, False, 0.0
    original_score = _lyric_garble_score(text)
    candidates = [text]
    # UTF-8 bytes misread as Latin-1 or Windows-1252, then decoded as UTF-8.
    for enc in ("latin-1", "cp1252"):
        try:
            raw = text.encode(enc, errors="strict")
            for dec in ("utf-8", "gb18030"):
                try:
                    candidates.append(raw.decode(dec, errors="strict"))
                except (UnicodeDecodeError, LookupError):
                    pass
        except (UnicodeEncodeError, LookupError):
            pass
    # GBK/GB18030 text incorrectly decoded as UTF-8 replacement characters is not
    # recoverable from replacement characters alone; skip risky guesses.
    best = min(candidates, key=lambda x: (_lyric_garble_score(x), x.count("\ufffd")))
    best_score = _lyric_garble_score(best)
    if best != text and best_score < original_score and "\ufffd" not in best:
        return best, True, max(0.0, original_score - best_score)
    return text, False, 0.0


def _normalize_match_text(value):
    value = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", value)


def find_lyric_source(target, directory_files):
    """Fast sibling search: shortlist by filename before reading any audio tags."""
    from difflib import SequenceMatcher
    logger = logging.getLogger("MusicImporter")
    target_stem = P(target).stem
    target_norm = _normalize_match_text(target_stem)
    target_tags = read_tags(target)
    target_title = _normalize_match_text(target_tags.get("title") or target_stem)
    target_artist = _normalize_match_text(target_tags.get("artist"))

    # Name-only shortlist avoids opening every audio file's metadata in large folders.
    shortlist = []
    for candidate in directory_files:
        if P(candidate).resolve() == P(target).resolve():
            continue
        cand_stem = P(candidate).stem
        cand_norm = _normalize_match_text(cand_stem)
        similarity = SequenceMatcher(None, target_norm, cand_norm).ratio() if target_norm and cand_norm else 0
        exact_stem = cand_stem.casefold() == target_stem.casefold()
        if exact_stem or (target_norm and cand_norm and (target_norm in cand_norm or cand_norm in target_norm)) or similarity >= 0.62:
            shortlist.append((4 if exact_stem else similarity, candidate))
    shortlist.sort(key=lambda x: x[0], reverse=True)
    shortlist = shortlist[:12]
    logger.info("LYRICS SOURCE SEARCH | target=%s | directory_audio=%d | shortlisted=%d", target, len(directory_files), len(shortlist))

    ranked = []
    for rank, candidate in shortlist:
        logger.info("LYRICS SOURCE CANDIDATE | target=%s | candidate=%s | filename_score=%.3f", target, candidate, rank)
        candidate_tags = read_tags(candidate)
        title = _normalize_match_text(candidate_tags.get("title") or P(candidate).stem)
        artist = _normalize_match_text(candidate_tags.get("artist"))
        score = 0
        if P(candidate).stem.lower() == P(target).stem.lower():
            score += 4
        if target_title and title == target_title:
            score += 5
        if target_artist and artist and artist == target_artist:
            score += 3
        if score >= 5:
            lyrics = read_lyrics(candidate)
            if lyrics:
                ranked.append((score, candidate, lyrics))
                logger.info("LYRICS SOURCE HAS TEXT | candidate=%s | score=%d | chars=%d", candidate, score, len(lyrics))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if ranked and (len(ranked) == 1 or ranked[0][0] > ranked[1][0]):
        logger.info("LYRICS SOURCE SELECTED | target=%s | source=%s | score=%d", target, ranked[0][1], ranked[0][0])
        return ranked[0][1], ranked[0][2]
    logger.info("LYRICS SOURCE NOT FOUND OR AMBIGUOUS | target=%s | matches=%d", target, len(ranked))
    return None, ""



def _strip_lrc_timestamps(text):
    """Convert timestamped LRC into plain lyrics for embedded tag compatibility."""
    lines = []
    for line in (text or "").splitlines():
        line = re.sub(r"\[(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.]\d{1,3})?\]", "", line)
        line = re.sub(r"\[(?:ti|ar|al|by|re|ve|offset):[^]]*\]", "", line, flags=re.I)
        if line.strip():
            lines.append(line.strip())
    return "\n".join(lines).strip()


def _audio_duration_seconds(path):
    try:
        from mutagen import File as MutagenFile
        audio = MutagenFile(str(path))
        return float(audio.info.length) if audio and getattr(audio, "info", None) else None
    except Exception:
        return None


def _lrclib_request_json(url, timeout=12):
    """Request LRCLIB JSON with a descriptive user agent; safely handle HTTP errors."""
    req = urllib.request.Request(url, headers={"User-Agent": "MusicImporter/1.0 (local music utility)"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def _score_lrclib_candidate(candidate, title, artist, album, duration):
    """Score search candidates using normalized metadata and duration; reject weak matches."""
    from difflib import SequenceMatcher
    ct = _normalize_match_text(candidate.get("trackName") or candidate.get("name") or "")
    ca = _normalize_match_text(candidate.get("artistName") or "")
    cal = _normalize_match_text(candidate.get("albumName") or "")
    nt, na, nal = _normalize_match_text(title), _normalize_match_text(artist), _normalize_match_text(album)
    title_ratio = SequenceMatcher(None, nt, ct).ratio() if nt and ct else 0.0
    artist_ratio = SequenceMatcher(None, na, ca).ratio() if na and ca else 0.0
    album_ratio = SequenceMatcher(None, nal, cal).ratio() if nal and cal else 0.0
    candidate_duration = candidate.get("duration")
    duration_delta = abs(float(candidate_duration) - float(duration)) if duration and candidate_duration else None
    # Duration is a key version discriminator. Reject wildly different recordings.
    if duration_delta is not None and duration_delta > max(12, float(duration) * 0.08):
        return -1, (title_ratio, artist_ratio, album_ratio, duration_delta)
    score = title_ratio * 5 + artist_ratio * 4 + album_ratio * 1.5
    if duration_delta is not None:
        score += max(0, 3 - duration_delta / 2)
    return score, (title_ratio, artist_ratio, album_ratio, duration_delta)


def fetch_lrclib_lyrics(path, timeout=12):
    """Get lyrics using exact-signature first, then metadata search as a fallback."""
    logger = logging.getLogger("MusicImporter")
    tags = read_tags(path)
    title = (tags.get("title") or P(path).stem).strip()
    artist = (tags.get("artist") or "").strip()
    album = (tags.get("album") or "").strip()
    duration = _audio_duration_seconds(path)
    if not title or not artist:
        return None, None, "缺少歌名或歌手标签，跳过在线匹配"

    base_params = {"track_name": title, "artist_name": artist}
    if album:
        base_params["album_name"] = album
    if duration and 1 <= duration <= 3600:
        base_params["duration"] = str(round(duration))

    # 1) Exact signature endpoint (duration helps distinguish versions).
    exact_url = "https://lrclib.net/api/get?" + urllib.parse.urlencode(base_params)
    logger.info("LYRICS ONLINE EXACT REQUEST | title=%s | artist=%s | album=%s | duration=%s", title, artist, album, duration)
    payload = None
    exact_error = None
    try:
        payload = _lrclib_request_json(exact_url, timeout)
        if not isinstance(payload, dict):
            payload = None
    except Exception as exc:
        exact_error = str(exc)
        logger.warning("LYRICS ONLINE EXACT FAILED | title=%s | reason=%s", title, exc)

    # 2) Search endpoint fallback. Search candidates are ranked rather than trusting first result.
    if not payload:
        search_params = {"track_name": title, "artist_name": artist}
        if album:
            search_params["album_name"] = album
        search_url = "https://lrclib.net/api/search?" + urllib.parse.urlencode(search_params)
        logger.info("LYRICS ONLINE SEARCH REQUEST | title=%s | artist=%s", title, artist)
        try:
            candidates = _lrclib_request_json(search_url, timeout)
            if isinstance(candidates, list):
                ranked = []
                for candidate in candidates:
                    if not isinstance(candidate, dict):
                        continue
                    score, metrics = _score_lrclib_candidate(candidate, title, artist, album, duration)
                    logger.info("LYRICS ONLINE CANDIDATE | title=%s | artist=%s | score=%.2f | metrics=%s | synced=%s | plain=%s",
                                candidate.get("trackName"), candidate.get("artistName"), score, metrics,
                                bool(candidate.get("syncedLyrics")), bool(candidate.get("plainLyrics")))
                    if score >= 7.5 and metrics[0] >= 0.72 and metrics[1] >= 0.60:
                        ranked.append((score, candidate))
                ranked.sort(key=lambda x: x[0], reverse=True)
                if ranked:
                    payload = ranked[0][1]
        except Exception as exc:
            logger.warning("LYRICS ONLINE SEARCH FAILED | title=%s | reason=%s", title, exc)
            if not exact_error:
                exact_error = str(exc)

    if not isinstance(payload, dict):
        return None, None, f"在线匹配未找到可靠结果{('：' + exact_error) if exact_error else ''}"

    matched_title = str(payload.get("trackName") or payload.get("name") or "")
    matched_artist = str(payload.get("artistName") or "")
    score, metrics = _score_lrclib_candidate(payload, title, artist, album, duration)
    if score < 7.5 or metrics[0] < 0.72 or metrics[1] < 0.60:
        logger.warning("LYRICS ONLINE REJECT | target=%s | match_title=%s | match_artist=%s | score=%.2f | metrics=%s", path, matched_title, matched_artist, score, metrics)
        return None, None, f"匹配结果不可靠（{matched_title} / {matched_artist}），已拒绝写入"

    synced = (payload.get("syncedLyrics") or "").strip()
    plain = (payload.get("plainLyrics") or "").strip()
    if synced and not plain:
        plain = _strip_lrc_timestamps(synced)
    if not plain and not synced:
        return None, None, "匹配到歌曲，但该条目没有歌词内容"
    logger.info("LYRICS ONLINE MATCH | title=%s | artist=%s | synced=%s | plain_chars=%d | synced_chars=%d | score=%.2f",
                matched_title, matched_artist, bool(synced), len(plain), len(synced), score)
    return plain or None, synced or None, {"title": matched_title, "artist": matched_artist, "album": payload.get("albumName", ""), "duration": payload.get("duration"), "score": score}

def save_synced_lrc(path, synced):
    """Write synced lyrics as a sibling .lrc sidecar without touching source audio."""
    if not synced:
        return None
    lrc_path = str(P(path).with_suffix(".lrc"))
    P(lrc_path).write_text(synced.rstrip() + "\n", encoding="utf-8")
    return lrc_path

def online_match_lyrics(target):
    """Match lyrics online and immediately apply them to the selected audio file."""
    logger = logging.getLogger("MusicImporter")
    started = time.monotonic()
    logger.info("LYRICS MATCH START | target=%s", target)
    plain, synced, info = fetch_lrclib_lyrics(target)
    if not plain and synced:
        plain = _strip_lrc_timestamps(synced)
    if not plain:
        detail = info if isinstance(info, str) else "匹配结果没有可写入的歌词"
        logger.info("LYRICS MATCH RESULT | target=%s | result=not-found | detail=%s", target, detail)
        return False, detail
    if not write_lyrics(target, plain):
        logger.error("LYRICS MATCH WRITE FAILED | target=%s", target)
        return False, "匹配到歌词，但写入音频标签失败"
    lrc_path = save_synced_lrc(target, synced) if synced else None
    matched = info if isinstance(info, dict) else {}
    detail = f"匹配成功并已写入：{matched.get('title', '')} / {matched.get('artist', '')}"
    if lrc_path:
        detail += f"；同步歌词保存为 {P(lrc_path).name}"
    else:
        detail += "；未提供同步时间轴"
    logger.info("LYRICS MATCH DONE | target=%s | title=%s | artist=%s | synced=%s | elapsed=%.2fs",
                target, matched.get("title", ""), matched.get("artist", ""), bool(synced), time.monotonic()-started)
    return True, detail


def normalize_track_number(raw):
    """Normalize common '03/12' forms to '3'; retain non-numeric content if unknown."""
    raw = (raw or "").strip()
    match = re.match(r"^(\d+)", raw)
    return str(int(match.group(1))) if match else raw


def parse_time_value(raw):
    """Parse split-preview time as seconds, MM:SS, or HH:MM:SS."""
    text = (raw or "").strip()
    if ":" not in text:
        return float(text)
    parts = text.split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        return int(minutes) * 60 + float(seconds)
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    raise ValueError("时间格式请使用秒数、MM:SS 或 HH:MM:SS")


def clean_title(title, artist="", filename=""):
    """Clean a title. Prefer a clear track-number/artist/title structure in the filename."""
    text = (title or "").strip()
    track_num = ""

    # Normalize full-width characters and common dash variants before parsing.
    dash_chars = "-－–—−﹣ｰ゠"
    separator_chars = re.escape(dash_chars + ":：")
    if filename:
        stem = unicodedata.normalize("NFKC", P(filename).stem).strip()
        # Match: track number + any artist text + separator + title.
        # The separator must occur after the artist text; the title must not be empty.
        filename_match = re.match(
            rf"^([0-9]{{1,3}})\s*(.*?)\s*[{separator_chars}]\s*(.+?)\s*$", stem
        )
        if filename_match:
            candidate_track, candidate_artist, candidate_title = filename_match.groups()
            if candidate_artist.strip() and candidate_title.strip():
                track_num = candidate_track
                text = candidate_title.strip()

    # Fallback: extract track prefix from tag title if filename parsing did not succeed.
    if not track_num:
        normalized_title = unicodedata.normalize("NFKC", text)
        prefix_match = TRACK_PREFIX.match(normalized_title)
        if prefix_match:
            track_num = next((g for g in prefix_match.groups() if g), "")
            text = normalized_title[prefix_match.end():]

    # Remove artist prefix only when tag title itself clearly contains it.
    artist = unicodedata.normalize("NFKC", (artist or "").strip())
    text = unicodedata.normalize("NFKC", text)
    if artist:
        text = re.sub(r"^\s*" + re.escape(artist) + r"\s*[-－–—−﹣ｰ゠:：]\s*", "", text, flags=re.I)

    for pattern in REMOVE_PATTERNS:
        text = re.sub(pattern, "", text, flags=re.I)
    text = re.sub(r"\.(?:flac|wav|ape|mp3|aac|alac|dsf|dff|aiff|aif|wv|ogg|opus)$", "", text, flags=re.I)
    text = re.sub(r"^\s*(?:[-_.、]\s*)+", "", text)
    text = re.sub(r"\s*(?:[-_.])\s*$", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip("[](){} _-.")
    return text, track_num


def safe_component(text, fallback):
    text = (text or fallback).strip()
    text = re.sub(r'[\\/:*?"<>|]', "_", text)
    return text[:160] or fallback



def _decode_cue_text(path):
    """Decode CUE text using strict, common encodings so Chinese tags are not silently corrupted."""
    raw = P(path).read_bytes()
    # BOMs first; UTF-8 is common, then Windows Chinese encodings used by older music releases.
    candidates = ("utf-8-sig", "utf-8", "gb18030", "big5", "cp932")
    for encoding in candidates:
        try:
            text = raw.decode(encoding, errors="strict")
            # Reject obviously mojibake-heavy text only as a last resort heuristic.
            return text, encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1 (fallback)"


def parse_cue_file(cue_path):
    """Parse CUE FILE/TRACK/TITLE/PERFORMER/INDEX 01 with multi-file support."""
    text, encoding = _decode_cue_text(cue_path)
    tracks = []
    current = None
    album_title = ""
    album_artist = ""
    file_name = None
    for raw in text.splitlines():
        line = raw.strip()
        m = re.match(r'^FILE\s+(?:"([^"]+)"|(.+?))\s+([A-Za-z0-9]+)\s*$', line, re.I)
        if m:
            file_name = (m.group(1) or m.group(2)).strip()
            continue
        m = re.match(r'^(TITLE|PERFORMER)\s+(?:"(.*)"|(.*))$', line, re.I)
        if m and current is None:
            val = (m.group(2) if m.group(2) is not None else m.group(3)).strip()
            if m.group(1).upper() == "TITLE": album_title = val
            else: album_artist = val
            continue
        m = re.match(r'^TRACK\s+(\d+)\s+([A-Za-z0-9]+)', line, re.I)
        if m:
            current = {"track": int(m.group(1)), "title": f"Track {int(m.group(1)):02d}",
                       "artist": album_artist, "start": None, "file": file_name}
            tracks.append(current)
            continue
        if current is not None:
            m = re.match(r'^(TITLE|PERFORMER)\s+(?:"(.*)"|(.*))$', line, re.I)
            if m:
                val = (m.group(2) if m.group(2) is not None else m.group(3)).strip()
                current["title" if m.group(1).upper() == "TITLE" else "artist"] = val
                continue
            m = re.match(r'^INDEX\s+01\s+(\d+):(\d+):(\d+)', line, re.I)
            if m:
                mm, ss, ff = map(int, m.groups())
                current["start"] = mm * 60 + ss + ff / 75.0
    if not tracks or any(t["start"] is None for t in tracks):
        raise ValueError("CUE 文件中未找到有效 TRACK / INDEX 01 信息。")
    # Group source file references. Most album CUE sheets reference one file, but split-file CUEs exist.
    cue_files = list(dict.fromkeys(t.get("file") for t in tracks if t.get("file")))
    return {"album": album_title, "artist": album_artist, "file": file_name,
            "files": cue_files, "encoding": encoding, "tracks": tracks}


class MusicImporter:
    def __init__(self, root):
        self.root = root
        root.title("Music Importer")
        root.geometry("1180x720")
        root.minsize(900, 580)

        self.ffmpeg = find_ffmpeg()
        if self.ffmpeg:
            try:
                probe = subprocess.run([self.ffmpeg, "-hide_banner", "-version"], capture_output=True, text=True, timeout=15)
                logging.getLogger("MusicImporter").info("FFmpeg version/build output:\n%s", (probe.stdout or probe.stderr)[:12000])
                logging.getLogger("MusicImporter").info("FFmpeg version probe exit code=%s", probe.returncode)
            except Exception:
                logging.getLogger("MusicImporter").exception("FFmpeg version probe failed")
        self.output_dir = str(P.home() / "Music" / "MusicImporter")
        self.output_dir_customized = False
        self.output_format = tk.StringVar(value="ALAC")
        self.auto_import = tk.BooleanVar(value=False)
        self.items = []
        self.cue_files = {}  # Associate selected/imported CUE files with matching audio files.
        self.split_plans = {}  # Per-source edited split plans, prepared before splitting.
        self.busy = False
        self.cancel_requested = threading.Event()
        self.current_process = None
        self.closing_requested = False
        self.build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.set_status("就绪" if self.ffmpeg else "未检测到 FFmpeg：请安装后重新启动")

    def build_ui(self):
        # This version uses one consistent light theme; theme switching is removed.
        self.ui = self._palette("Light")
        c = self.ui
        self.root.title("Music Importer")
        self.root.geometry("1280x820")
        self.root.minsize(1020, 680)
        self.root.configure(bg=c["bg"])

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        # Pick installed fonts so the design looks polished without requiring font installs.
        available_fonts = set(self.root.tk.call("font", "families"))
        def font_family(candidates, fallback="Helvetica"):
            return next((name for name in candidates if name in available_fonts), fallback)
        font_ui = font_family(["SF Pro Text", "SF Pro", "Avenir Next", "Inter", "Helvetica Neue"])
        font_display = font_family(["SF Pro Display", "Avenir Next", "Inter", "Helvetica Neue"], font_ui)
        self.font_ui, self.font_display = font_ui, font_display
        style.configure("TFrame", background=c["bg"])
        style.configure("Panel.TFrame", background=c["panel"])
        style.configure("Panel2.TFrame", background=c["panel2"])
        style.configure("TLabel", background=c["bg"], foreground=c["text"], font=(font_ui, 10))
        style.configure("Panel.TLabel", background=c["panel"], foreground=c["text"])
        style.configure("Muted.TLabel", background=c["bg"], foreground=c["muted"])
        style.configure("PanelMuted.TLabel", background=c["panel"], foreground=c["muted"])
        style.configure("Hero.TLabel", background=c["bg"], foreground=c["text"], font=(font_display, 24, "bold"))
        style.configure("HeroSub.TLabel", background=c["bg"], foreground=c["muted"], font=(font_ui, 10))
        style.configure("Accent.TLabel", background=c["bg"], foreground=c["text"], font=(font_ui, 10, "bold"))
        style.configure("Section.TLabel", background=c["panel"], foreground=c["text"], font=(font_ui, 10, "bold"))
        style.configure("TButton", background=c["panel2"], foreground=c["text"], bordercolor=c["line"],
                       padding=(12, 9), font=(font_ui, 10), relief="flat")
        style.map("TButton", background=[("active", c["line"]), ("disabled", c["panel"])],
                  foreground=[("disabled", c["muted"])], bordercolor=[("active", c["cyan"])])
        style.configure("Accent.TButton", background=c["panel2"], foreground=c["text"], bordercolor=c["cyan"],
                       padding=(15, 11), font=(font_ui, 10, "bold"), relief="flat")
        style.map("Accent.TButton", background=[("active", c["panel2"])], bordercolor=[("active", c["cyan"])])
        style.configure("Danger.TButton", background=c["panel2"], foreground=c["red"], bordercolor=c["red"],
                       padding=(12, 9), font=(font_ui, 10, "bold"), relief="flat")
        style.configure("Split.TButton", background=c["panel2"], foreground=c["text"], bordercolor=c["cyan"],
                       padding=(13, 10), font=(font_ui, 10, "bold"), relief="flat")
        style.map("Split.TButton", background=[("active", c["panel2"])], bordercolor=[("active", c["cyan"])])
        style.map("Danger.TButton", background=[("active", c["panel2"])], bordercolor=[("active", c["red"])])
        style.configure("TCheckbutton", background=c["panel"], foreground=c["text"], font=(font_ui, 10))
        style.map("TCheckbutton", background=[("active", c["panel"])], foreground=[("active", c["text"])])
        style.configure("TCombobox", fieldbackground=c["input"], background=c["panel2"], foreground=c["text"],
                       bordercolor=c["line"], arrowcolor=c["cyan"], padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", c["input"])], foreground=[("readonly", c["text"])])
        style.configure("Treeview", background=c["input"], fieldbackground=c["input"], foreground=c["text"],
                       rowheight=36, borderwidth=0, font=(font_ui, 10))
        style.configure("Treeview.Heading", background=c["panel2"], foreground=c["text"],
                       relief="flat", font=(font_ui, 9, "bold"), padding=(8, 10))
        style.map("Treeview", background=[("selected", "#173C50")], foreground=[("selected", "#FFFFFF")])
        style.map("Treeview.Heading", background=[("active", c["panel2"])])
        style.configure("Horizontal.TProgressbar", troughcolor=c["panel2"], background=c["cyan"],
                       bordercolor=c["panel2"], lightcolor=c["cyan"], darkcolor=c["cyan"], thickness=12)

        # Header: keep the product name plain, remove invented slogans/statuses.
        header = tk.Frame(self.root, bg=c["bg"])
        header.pack(fill="x", padx=22, pady=(16, 12))
        tk.Label(header, text="Music Importer", fg=c["text"], bg=c["bg"],
                 font=(font_display, 20, "bold")).pack(side="left")

        # Main work area: action rail and file table.
        workarea = tk.Frame(self.root, bg=c["bg"])
        workarea.pack(fill="both", expand=True, padx=22, pady=(0, 12))
        rail = tk.Frame(workarea, bg=c["panel"], highlightbackground=c["line"], highlightthickness=1, width=208)
        rail.pack(side="left", fill="y", padx=(0, 12)); rail.pack_propagate(False)
        # Keep the left rail focused: file intake, removal, and a compact tools menu.
        tk.Label(rail, text="操作菜单", fg=c["text"], bg=c["panel"], font=(font_ui, 11, "bold")).pack(anchor="w", padx=14, pady=(16, 10))
        self.add_menu_button = ttk.Button(rail, text="＋  添加音频  ▾", command=lambda: self._show_action_menu(self.add_menu_button, [
            ("添加音乐文件…", self.add_files), ("添加整个文件夹…", self.add_folder)]), style="Accent.TButton")
        self.add_menu_button.pack(fill="x", padx=10, pady=4)
        ttk.Button(rail, text="⌫  移除选中项目", command=self.remove_selected).pack(fill="x", padx=10, pady=4)
        self.tools_menu_button = ttk.Button(rail, text="标签与封面工具  ▾", command=lambda: self._show_action_menu(self.tools_menu_button, [
            ("歌名智能清理…", self.preview_cleanup), ("批量编辑信息…", self.batch_edit)]), style="TButton")
        self.tools_menu_button.pack(fill="x", padx=10, pady=4)
        ttk.Button(rail, text="拆分预览 / 编辑曲目", command=self.preview_split_tracks, style="Accent.TButton").pack(fill="x", padx=10, pady=4)
        self.lyrics_match_button = ttk.Button(rail, text="在线匹配歌词", command=self.match_selected_lyrics, style="Accent.TButton")
        self.lyrics_match_button.pack(fill="x", padx=10, pady=4)

        table_panel = tk.Frame(workarea, bg=c["panel"], highlightbackground=c["line"], highlightthickness=1)
        table_panel.pack(side="left", fill="both", expand=True)
        table_head = tk.Frame(table_panel, bg=c["panel"]); table_head.pack(fill="x", padx=14, pady=(12, 8))
        tk.Label(table_head, text="TRACK LIBRARY", fg=c["text"], bg=c["panel"], font=(font_display, 14, "bold")).pack(side="left")
        tk.Label(table_head, text="文件列表与元数据", fg=c["muted"], bg=c["panel"], font=(font_ui, 9)).pack(side="left", padx=(8, 0))
        cols = ("filename", "title", "artist", "album", "albumartist", "date", "track", "format", "cover", "status")
        self.tree = ttk.Treeview(table_panel, columns=cols, show="headings", selectmode="extended")
        heading = {"filename": "SOURCE FILE", "title": "TRACK TITLE", "artist": "ARTIST", "album": "ALBUM", "albumartist": "ALBUM ARTIST",
                   "date": "YEAR", "track": "NO.", "format": "AUDIO / QUALITY", "cover": "ARTWORK", "status": "STATUS"}
        widths = {"filename": 160, "title": 190, "artist": 125, "album": 145, "albumartist": 125,
                  "date": 62, "track": 50, "format": 125, "cover": 110, "status": 95}
        for col in cols:
            self.tree.heading(col, text=heading[col])
            self.tree.column(col, width=widths[col], minwidth=45, stretch=(col in {"title", "album"}))
        tree_y = ttk.Scrollbar(table_panel, orient="vertical", command=self.tree.yview)
        tree_x = ttk.Scrollbar(table_panel, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=tree_y.set, xscrollcommand=tree_x.set)
        tree_y.pack(side="right", fill="y", padx=(0, 8), pady=(0, 10))
        tree_x.pack(side="bottom", fill="x", padx=(10, 0), pady=(0, 8))
        self.tree.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=(0, 10))
        # Double-click a row in the main library to edit its metadata directly.
        def on_track_double_click(event):
            row = self.tree.identify_row(event.y)
            if not row:
                return
            self.tree.selection_set(row)
            self.edit_one()
        self.tree.bind("<Double-Button-1>", on_track_double_click)
        self.tree.tag_configure("odd", background=c["panel2"])
        self.tree.tag_configure("even", background=c["panel"])

        # Output path and format are secondary settings; keep them inside the left rail menu.
        self.output_path_display = tk.StringVar(value="")
        self.output_settings_button = ttk.Button(rail, text="输出与格式设置  ▾", command=self.show_output_settings_menu, style="TButton")
        self.output_settings_button.pack(fill="x", padx=10, pady=4)
        self.output_path_label = tk.Label(rail, textvariable=self.output_path_display, fg=c["muted"], bg=c["panel"],
                                         font=(font_ui, 8), justify="left", anchor="w", wraplength=175)
        # Hide default path; show only after the user explicitly changes it.

        controls = tk.Frame(self.root, bg=c["panel"], highlightbackground=c["line"], highlightthickness=1)
        controls.pack(fill="x", padx=22, pady=(0, 10))
        controls_inner = tk.Frame(controls, bg=c["panel"]); controls_inner.pack(fill="x", padx=12, pady=10)
        self.action_buttons = []
        # Persistent settings summary: users can see important defaults and changes at a glance.
        self.settings_summary = tk.Label(
            controls_inner, text="", anchor="w", bg=c["panel"], fg=c["muted"],
            font=(font_ui, 9), padx=2
        )
        self.settings_summary.pack(fill="x", pady=(0, 8))
        action_row = tk.Frame(controls_inner, bg=c["panel"])
        action_row.pack(fill="x")
        self.split_button = ttk.Button(action_row, text="拆分歌曲", command=lambda: self.start_work("split"), style="Accent.TButton")
        self.split_button.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.split_convert_button = ttk.Button(action_row, text="拆分并转换", command=lambda: self.start_work("split_convert"), style="Accent.TButton")
        self.split_convert_button.pack(side="left", fill="x", expand=True, padx=6)
        self.convert_button = ttk.Button(action_row, text="格式转换", command=lambda: self.start_work("convert"), style="Accent.TButton")
        self.convert_button.pack(side="left", fill="x", expand=True, padx=6)
        self.action_buttons.extend([self.split_button, self.split_convert_button, self.convert_button, self.lyrics_match_button])
        self.stop_button = ttk.Button(action_row, text="停止任务", command=self.stop_work, state="disabled", style="Danger.TButton")
        self.stop_button.pack(side="left", fill="x", expand=True, padx=(6, 0))
        # Apple Music auto-add is a secondary preference, housed in the output settings menu.
        self._update_settings_summary()

        status_panel = tk.Frame(self.root, bg=c["bg"])
        status_panel.pack(fill="x", padx=22, pady=(0, 12))
        status_line = tk.Frame(status_panel, bg=c["bg"])
        status_line.pack(fill="x", pady=(0, 5))
        self.status = tk.Label(status_line, text="", anchor="w", bg=c["bg"], fg=c["muted"], font=(font_ui, 9))
        self.status.pack(side="left", fill="x", expand=True)
        self.progress_percent = tk.Label(status_line, text="0%", bg=c["bg"], fg=c["text"], font=(font_ui, 9, "bold"))
        self.progress_percent.pack(side="right")
        progress_track = tk.Frame(status_panel, bg="#18263B", height=12, highlightbackground="#2C425D", highlightthickness=1)
        progress_track.pack(fill="x")
        progress_track.pack_propagate(False)
        self.progress_fill = tk.Frame(progress_track, bg=c["cyan"], height=10)
        self.progress_fill.place(x=0, y=0, relheight=1, width=0)
        self.progress = self._ProgressAdapter(self, progress_track, self.progress_fill)

    def _palette(self, mode):
        if mode == "Dark":
            return {"bg":"#17191D", "panel":"#22252B", "panel2":"#2C3037", "line":"#3A3F47",
                    "text":"#F0F2F4", "muted":"#A8AFB8", "cyan":"#27C7D9", "magenta":"#E55AB7",
                    "green":"#43C88A", "yellow":"#E7B85C", "red":"#E56C76", "input":"#191C21"}
        # Light theme: warm neutral surfaces with a teal accent, not sterile white.
        return {"bg":"#F4F6F8", "panel":"#FFFFFF", "panel2":"#E9EEF2", "line":"#D5DDE4",
                "text":"#202831", "muted":"#687581", "cyan":"#087F91", "magenta":"#087F91",
                "green":"#087F91", "yellow":"#687581", "red":"#C34450", "input":"#FFFFFF"}

    def _system_theme_mode(self):
        if os.name == "posix" and P("/System/Library").exists():
            try:
                result = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"],
                                       capture_output=True, text=True, timeout=2)
                return "Dark" if "Dark" in result.stdout else "Light"
            except Exception:
                return "Light"
        return "Light"

    def _prepare_popup(self, window, title, geometry, parent=None):
        """Apply the same light/dark surface, typography, spacing and modal behavior to every custom popup."""
        c = self.ui
        window.title(title)
        window.geometry(geometry)
        window.configure(bg=c["bg"])
        window.transient(parent or self.root)
        try:
            window.resizable(True, True)
        except tk.TclError:
            pass
        # Use shared popup styles; avoid standalone default Tk grey surfaces.
        style = ttk.Style(window)
        style.configure("Popup.TFrame", background=c["bg"])
        style.configure("PopupPanel.TFrame", background=c["panel"])
        style.configure("Popup.TLabel", background=c["bg"], foreground=c["text"], font=(self.font_ui, 10))
        style.configure("PopupMuted.TLabel", background=c["bg"], foreground=c["muted"], font=(self.font_ui, 9))
        style.configure("Popup.TButton", padding=(12, 7), font=(self.font_ui, 10))
        style.configure("PopupAccent.TButton", padding=(14, 8), font=(self.font_ui, 10, "bold"),
                       background=c["panel2"], foreground=c["text"], bordercolor=c["cyan"])
        return c

    def _popup_body(self, window, padding=14):
        frame = ttk.Frame(window, style="Popup.TFrame", padding=padding)
        frame.pack(fill="both", expand=True)
        return frame

    def show_output_settings_menu(self):
        """Compact output settings menu including Apple Music auto-add toggle."""
        button = self.output_settings_button
        c = self.ui
        menu = tk.Menu(button, tearoff=0, bg=c["panel"], fg=c["text"],
                      activebackground=c["panel2"], activeforeground=c["text"],
                      font=(self.font_ui, 10), relief="flat", borderwidth=1)
        menu.add_command(label="更改输出目录…", command=self.choose_output)
        menu.add_command(label="打开输出目录", command=self.open_output)
        menu.add_command(label="设置输出格式…", command=self.choose_format)
        menu.add_separator()
        menu.add_checkbutton(label="完成后自动加入 Apple Music", variable=self.auto_import,
                            command=self.toggle_auto_import, foreground=c["text"],
                            activeforeground=c["text"], background=c["panel"],
                            activebackground=c["panel2"])
        menu.tk_popup(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())

    def _show_action_menu(self, button, actions):
        """Show a compact popup menu for secondary actions, using the active theme."""
        c = self.ui
        menu = tk.Menu(button, tearoff=0, bg=c["panel"], fg=c["text"],
                      activebackground=c["panel2"], activeforeground=c["text"],
                      font=(self.font_ui, 10), relief="flat", borderwidth=1)
        for label, command in actions:
            menu.add_command(label=label, command=command)
        x = button.winfo_rootx()
        y = button.winfo_rooty() + button.winfo_height()
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    class _ProgressAdapter:
        """Drop-in configure adapter for the custom glowing progress track."""
        def __init__(self, owner, track, fill):
            self.owner, self.track, self.fill = owner, track, fill
            self.maximum, self.value = 100, 0
        def configure(self, **kwargs):
            self.maximum = max(1, float(kwargs.get("maximum", self.maximum)))
            self.value = max(0, min(float(kwargs.get("value", self.value)), self.maximum))
            ratio = self.value / self.maximum
            width = max(0, int(self.track.winfo_width() * ratio))
            self.fill.place(x=0, y=0, relheight=1, width=width)
            self.fill.configure(bg=self.owner.ui["cyan"])
            self.owner.progress_percent.configure(text=f"{int(ratio * 100)}%")
            self.track.update_idletasks()
            self.track.bind("<Configure>", lambda _e: self.configure())

    @staticmethod
    def _audio_quality_badge(path):
        ext = P(path).suffix.lower().lstrip(".")
        lossless = {"flac", "wav", "ape", "aiff", "aif", "dsf", "dff", "wv", "m4a"}
        lossy = {"mp3", "aac", "ogg", "opus"}
        if ext in {"dsf", "dff"}:
            return f"{ext.upper()}  ·  DSD"
        if ext in lossless:
            return f"{ext.upper()}  ·  LOSSLESS"
        if ext in lossy:
            return f"{ext.upper()}  ·  LOSSY"
        return ext.upper() or "AUDIO"

    def _format_loss_label(self, fmt=None):
        fmt = (fmt or self.output_format.get()).upper()
        return "无损" if fmt in {"ALAC", "FLAC", "WAV", "AIFF"} else "有损"

    def _update_settings_summary(self):
        """Refresh a concise, always-visible summary of current/default settings."""
        if not hasattr(self, "settings_summary"):
            return
        output = self.output_dir if getattr(self, "output_dir_customized", False) else "默认：~/Music/MusicImporter"
        import_state = "开启" if self.auto_import.get() else "关闭"
        self.settings_summary.configure(
            text=f"当前设置  ·  输出格式：{self.output_format.get()}（{self._format_loss_label()}）  ·  输出位置：{output}  ·  Apple Music 自动加入：{import_state}"
        )

    def set_status(self, text):
        self.root.after(0, lambda: self.status.config(text=text))

    def add_files(self):
        paths = filedialog.askopenfilenames(
            title="选择音频文件",
            filetypes=[("音频与 CUE", "*.flac *.wav *.ape *.aiff *.aif *.dsf *.dff *.wv *.mp3 *.m4a *.aac *.ogg *.opus *.cue"), ("所有文件", "*.*")]
        )
        self.add_paths(paths)

    def add_folder(self):
        folder = filedialog.askdirectory(title="选择音乐文件夹")
        if not folder:
            return
        paths = [str(p) for p in P(folder).rglob("*") if p.is_file() and (p.suffix.lower() in SUPPORTED or p.suffix.lower() == ".cue")]
        # Keep artwork lookup inside the folder explicitly selected by the user.
        self.add_paths(paths, artwork_root=folder)

    def add_paths(self, paths, artwork_root=None):
        # CUE files are auxiliary split metadata; associate them with audio by basename and folder.
        audio_paths = []
        cue_paths = []
        for path in paths:
            path = str(path)
            if P(path).suffix.lower() == ".cue":
                cue_paths.append(path)
                continue
            if P(path).suffix.lower() in SUPPORTED:
                audio_paths.append(path)
        # Match CUE to explicitly selected audio by its FILE reference, or same basename.
        for cue_path in cue_paths:
            cue_stem_key = str(P(cue_path).parent / P(cue_path).stem
            )
            try:
                cue_info = parse_cue_file(cue_path)
                references = {P(name).name.lower() for name in cue_info.get("files", [])}
            except Exception:
                references = set()
            associated = [audio for audio in audio_paths if P(audio).name.lower() in references]
            if not associated:
                same_stem = [audio for audio in audio_paths if P(audio).stem.lower() == P(cue_path).stem.lower()]
                associated = same_stem
            if associated:
                for audio in associated: self.cue_files[str(audio)] = cue_path
            else:
                # Save a stem-level association so a later add of the audio can discover it.
                self.cue_files[cue_stem_key] = cue_path
        # Auto-match sidecar CUEs (case-insensitive suffix, same stem).
        existing = {x["source"] for x in self.items}
        for path in audio_paths:
            path = str(path)
            if path in existing:
                continue
            if path not in self.cue_files:
                cue = P(path).with_suffix(".cue")
                if cue.exists(): self.cue_files[path] = str(cue)
                else:
                    cue_upper = P(path).with_suffix(".CUE")
                    if cue_upper.exists(): self.cue_files[path] = str(cue_upper)
            tags = read_tags(path)
            # If track tag is empty, extract a likely track number from the filename.
            match = TRACK_PREFIX.match(P(path).stem)
            if not tags["tracknumber"] and match:
                tags["tracknumber"] = normalize_track_number(match.group(1) or match.group(2))
            # Prefer a cover image found beside this track by default. Searching
            # is performed per audio file's own directory, so nested album folders
            # keep their local artwork instead of inheriting the parent folder's.
            folder_cover = self.find_folder_artwork(path, search_root=artwork_root or P(path).parent)
            self.items.append({
                "source": path, "tags": tags, "output": None, "status": "待转换",
                "artwork": folder_cover,
                "artwork_source": f"自动识别：{P(folder_cover).name}" if folder_cover else "未找到外部封面（保留内嵌封面）"
            })
            existing.add(path)
        self.refresh()

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for idx, item in enumerate(self.items):
            t = item["tags"]
            self.tree.insert("", "end", iid=str(idx), values=(
                P(item["source"]).name, t["title"], t["artist"], t["album"],
                t["albumartist"], t["date"], t["tracknumber"],
                self._audio_quality_badge(item["source"]),
                ("有：" + P(item["artwork"]).name if item.get("artwork") else "保留内嵌"),
                item["status"]
            ), tags=("odd" if idx % 2 else "even",))

    def selected_indices(self):
        return [int(i) for i in self.tree.selection()]

    def remove_selected(self):
        for idx in sorted(self.selected_indices(), reverse=True):
            del self.items[idx]
        self.refresh()

    def preview_cleanup(self):
        indices = self.selected_indices() or list(range(len(self.items)))
        if not indices:
            messagebox.showinfo("提示", "请先添加音乐文件。")
            return

        win = tk.Toplevel(self.root)
        self._prepare_popup(win, "歌名预处理预览", "760x440")
        body = self._popup_body(win, 12)
        table = ttk.Treeview(body, columns=("filename", "old", "new", "track"), show="headings")
        table.heading("filename", text="源文件名（不含路径）")
        table.heading("old", text="当前标签歌名")
        table.heading("new", text="清理后歌名")
        table.heading("track", text="提取曲序")
        table.column("filename", width=260)
        table.column("old", width=200)
        table.column("new", width=200)
        table.column("track", width=70)
        table.pack(fill="both", expand=True)

        proposals = {}
        changed = 0
        for idx in indices:
            old = self.items[idx]["tags"]["title"]
            new, extracted_track = clean_title(old, self.items[idx]["tags"].get("artist", ""), self.items[idx]["source"])
            existing_track = self.items[idx]["tags"]["tracknumber"]
            proposed_track = existing_track or extracted_track
            proposals[idx] = (new, proposed_track)
            source_name = P(self.items[idx]["source"]).name
            table.insert("", "end", iid=str(idx), values=(source_name, old, new, proposed_track))
            if new != old or (not existing_track and extracted_track):
                changed += 1

        ttk.Label(body, text=f"{len(indices)} 首歌曲 · {changed} 首预计修改", style="PopupMuted.TLabel").pack(anchor="w", pady=(8, 0))

        def apply_changes():
            for idx, (new, track) in proposals.items():
                self.items[idx]["tags"]["title"] = new
                if not self.items[idx]["tags"]["tracknumber"] and track:
                    self.items[idx]["tags"]["tracknumber"] = track
            self.refresh()
            win.destroy()

        row = ttk.Frame(body, style="Popup.TFrame")
        row.pack(fill="x", pady=(12, 0))
        ttk.Button(row, text="取消", command=win.destroy, style="Popup.TButton").pack(side="right", padx=(6, 0))
        ttk.Button(row, text="应用", command=apply_changes, style="PopupAccent.TButton").pack(side="right")

    def batch_edit(self):
        indices = self.selected_indices()
        if not indices:
            messagebox.showinfo("提示", "请先选中需要批量修改的歌曲。")
            return
        win = tk.Toplevel(self.root)
        self._prepare_popup(win, "批量编辑歌曲信息", "520x390")
        body = self._popup_body(win, 14)

        editable = [
            ("artist", "歌手 Artist"),
            ("albumartist", "专辑艺术家 Album Artist"),
            ("album", "专辑 Album"),
            ("date", "年份 Date"),
            ("genre", "流派 Genre"),
        ]
        entries = {}
        for key, label in editable:
            frame = ttk.Frame(body, style="Popup.TFrame")
            frame.pack(fill="x", pady=5)
            ttk.Label(frame, text=label, width=20, style="Popup.TLabel").pack(side="left")
            entry = ttk.Entry(frame)
            entry.pack(side="left", fill="x", expand=True)
            entries[key] = entry

        # Collect image choices from folders represented by the selected tracks.
        # Empty choice means no external cover: keep embedded source art only.
        image_exts = {".jpg", ".jpeg", ".png", ".webp"}
        folder_images = {}
        for idx in indices:
            folder = P(self.items[idx]["source"]).parent
            try:
                for image_file in folder.iterdir():
                    if image_file.is_file() and image_file.suffix.lower() in image_exts:
                        folder_images[str(image_file)] = image_file.name
            except OSError:
                pass
        cover_choices = [""] + sorted(folder_images.keys(), key=lambda x: (folder_images[x].lower(), x.lower()))
        cover_frame = ttk.Frame(body, style="Popup.TFrame")
        cover_frame.pack(fill="x", pady=(10, 4))
        ttk.Label(cover_frame, text="批量封面", width=20, style="Popup.TLabel").pack(side="left")
        cover_var = tk.StringVar(value="")
        cover_combo = ttk.Combobox(cover_frame, state="readonly", values=["（不指定封面）"] + [folder_images[x] for x in cover_choices[1:]], textvariable=cover_var)
        cover_combo.pack(side="left", fill="x", expand=True)
        cover_combo.current(0)
        cover_choice_map = {"（不指定封面）": ""}
        for path in cover_choices[1:]:
            # Disambiguate same-named images in different folders in dropdown.
            display = folder_images[path]
            if display in cover_choice_map:
                display = f"{display} · {P(path).parent.name}"
            cover_choice_map[display] = path
        cover_combo.configure(values=list(cover_choice_map.keys()))

        def apply():
            values = {k: e.get().strip() for k, e in entries.items()}
            chosen_cover = cover_choice_map.get(cover_var.get(), "")
            for idx in indices:
                for key, val in values.items():
                    if val:
                        self.items[idx]["tags"][key] = val
                self.items[idx]["artwork"] = chosen_cover or None
                self.items[idx]["artwork_source"] = "批量指定" if chosen_cover else "未指定（保留内嵌封面）"
            self.refresh()
            win.destroy()

        ttk.Button(body, text="应用修改", command=apply, style="PopupAccent.TButton").pack(anchor="e", pady=(12, 0))

    def edit_one(self):
        indices = self.selected_indices()
        if len(indices) != 1:
            messagebox.showinfo("提示", "请选中一首歌曲再编辑。")
            return
        idx = indices[0]
        tags = self.items[idx]["tags"]
        win = tk.Toplevel(self.root)
        self._prepare_popup(win, "编辑歌曲信息", "480x430")
        body = self._popup_body(win, 14)
        entries = {}
        for row, (key, label) in enumerate(TAG_FIELDS):
            ttk.Label(body, text=label, style="Popup.TLabel").grid(row=row, column=0, padx=(0, 10), pady=5, sticky="w")
            ent = ttk.Entry(body, width=42)
            ent.insert(0, tags.get(key, ""))
            ent.grid(row=row, column=1, padx=0, pady=5, sticky="ew")
            entries[key] = ent

        def save():
            for key, ent in entries.items():
                tags[key] = ent.get().strip()
            self.refresh()
            win.destroy()

        body.columnconfigure(1, weight=1)
        ttk.Button(body, text="保存", command=save, style="PopupAccent.TButton").grid(row=len(TAG_FIELDS), column=1, pady=(14, 0), sticky="e")

    def find_folder_artwork(self, audio_path, search_root=None):
        """Find artwork beside a track, searching upward only within search_root.

        This supports layouts such as:
            Neon/
              cover.png
              CD1/track01.flac
              CD2/track02.flac

        Artwork in the track's own directory always wins. Parent folders are
        checked only as a fallback, so a CD-specific cover is not overridden
        by a collection-level cover.
        """
        supported_images = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
        name_priority = ["cover", "folder", "front", "album", "albumart", "album_art",
                         "artwork", "art", "coverart", "cover_art", "scan", "image"]

        def choose_in_folder(folder):
            try:
                folder_files = [
                    p for p in folder.iterdir()
                    if p.is_file() and p.suffix.lower() in supported_images
                ]
            except OSError:
                return None
            if not folder_files:
                return None

            by_stem = {}
            for image in folder_files:
                by_stem.setdefault(image.stem.casefold(), []).append(image)
            for stem in name_priority:
                if stem in by_stem:
                    return str(sorted(by_stem[stem], key=lambda p: p.suffix.lower())[0])

            semantic = []
            for image in folder_files:
                low = image.stem.casefold().replace("-", " ").replace("_", " ")
                if any(word in low for word in ("cover", "folder", "front", "album", "artwork", "coverart")):
                    semantic.append(image)
            if semantic:
                semantic.sort(key=lambda p: (
                    next((i for i, word in enumerate(name_priority) if word in p.stem.casefold()), 99),
                    p.name.casefold()
                ))
                return str(semantic[0])

            if len(folder_files) == 1:
                return str(folder_files[0])
            try:
                from PIL import Image
                ranked = []
                for image in folder_files:
                    try:
                        with Image.open(image) as im:
                            ranked.append((im.width * im.height, image.name.casefold(), image))
                    except Exception:
                        continue
                if ranked:
                    ranked.sort(key=lambda x: (-x[0], x[1]))
                    return str(ranked[0][2])
            except Exception:
                pass
            return None

        track_folder = P(audio_path).resolve().parent
        boundary = P(search_root).resolve() if search_root else track_folder

        # Refuse to search outside the selected root. If the audio file is not
        # actually inside that root (for example, a stale path), fall back to
        # its own directory rather than traversing unrelated parent folders.
        try:
            track_folder.relative_to(boundary)
        except ValueError:
            boundary = track_folder

        folder = track_folder
        while True:
            found = choose_in_folder(folder)
            if found:
                return found
            if folder == boundary:
                break
            parent = folder.parent
            if parent == folder:
                break
            try:
                parent.relative_to(boundary)
            except ValueError:
                break
            folder = parent
        return None

    def extract_embedded_artwork(self, audio_path, destination_dir=None):
        """Extract the first embedded source image to a temporary JPEG/PNG file.
        Returns None if the audio has no embedded artwork or extraction fails.
        Caller is responsible for removing the returned temporary file.
        """
        try:
            from mutagen import File as MutagenFile
            from mutagen.id3 import APIC
            from mutagen.mp4 import MP4
            from mutagen.flac import FLAC
            from mutagen.oggvorbis import OggVorbis
            from mutagen.oggopus import OggOpus
            from mutagen.oggflac import OggFLAC
            audio = MutagenFile(str(audio_path))
            image_data = None
            image_ext = ".jpg"
            if audio is None:
                return None
            # ID3 (MP3/AIFF), MP4/M4A, FLAC picture blocks, and Vorbis-family
            # picture metadata are handled without relying on FFmpeg stream maps.
            tags = getattr(audio, "tags", None)
            if tags:
                for tag in tags.values():
                    if isinstance(tag, APIC):
                        image_data = tag.data
                        image_ext = ".jpg" if "jpeg" in (tag.mime or "").lower() else ".png"
                        break
                if image_data is None and "covr" in tags:
                    covr = tags["covr"]
                    if covr:
                        image_data = bytes(covr[0])
                        image_ext = ".jpg" if getattr(covr[0], "imageformat", 0) == 13 else ".png"
                if image_data is None and hasattr(audio, "pictures") and audio.pictures:
                    pic = audio.pictures[0]
                    image_data = pic.data
                    image_ext = ".jpg" if "jpeg" in (pic.mime or "").lower() else ".png"
                if image_data is None:
                    encoded = tags.get("metadata_block_picture") or tags.get("coverart")
                    if encoded:
                        import base64
                        from mutagen.flac import Picture
                        raw = base64.b64decode(encoded[0]) if isinstance(encoded[0], str) else encoded[0]
                        pic = Picture(raw) if isinstance(raw, bytes) and raw.startswith(b"\x00\x00\x00") else None
                        if pic:
                            image_data = pic.data
                            image_ext = ".jpg" if "jpeg" in (pic.mime or "").lower() else ".png"
            if not image_data:
                return None
            import tempfile
            folder = destination_dir or tempfile.gettempdir()
            fd, out_path = tempfile.mkstemp(prefix="musicimporter_embedded_", suffix=image_ext, dir=folder)
            with os.fdopen(fd, "wb") as f:
                f.write(image_data)
            return out_path
        except Exception:
            logging.getLogger("MusicImporter").exception("EMBEDDED ARTWORK EXTRACT FAILED | source=%s", audio_path)
            return None

    def choose_format(self):
        """Compact output-format selector, matching the application's light theme."""
        win = tk.Toplevel(self.root)
        c = self._prepare_popup(win, "输出格式", "360x180")
        body = self._popup_body(win, 18)
        ttk.Label(body, text="输出格式", style="Popup.TLabel").pack(anchor="w", pady=(0, 8))
        fmt = ttk.Combobox(body, state="readonly", values=("ALAC", "FLAC", "WAV", "AIFF", "MP3", "AAC", "Opus"), width=14)
        fmt.set(self.output_format.get()); fmt.pack(anchor="w")
        hint = ttk.Label(body, text="", style="PopupMuted.TLabel"); hint.pack(anchor="w", pady=(6, 0))
        def update_hint(_event=None):
            hint.configure(text="无损输出" if fmt.get() in {"ALAC", "FLAC", "WAV", "AIFF"} else "有损输出")
        fmt.bind("<<ComboboxSelected>>", update_hint); update_hint()
        actions = ttk.Frame(body, style="Popup.TFrame"); actions.pack(side="bottom", fill="x", pady=(14, 0))
        def apply():
            chosen = fmt.get().strip()
            self.output_format.set(chosen)
            # Close first so the user receives an immediate, visible confirmation.
            win.destroy()
            self._update_settings_summary()
            self.set_status(f"输出格式已应用：{chosen}（{self._format_loss_label(chosen)}）")
        ttk.Button(actions, text="取消", command=win.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(actions, text="应用", command=apply, style="PopupAccent.TButton").pack(side="right")
        win.transient(self.root); win.grab_set()

    def choose_output(self):
        folder = filedialog.askdirectory(title="选择输出目录")
        if folder:
            self.output_dir = folder
            self.output_dir_customized = True
            self.output_path_display.set("输出目录：\n" + folder)
            self.output_path_label.pack(anchor="w", padx=12, pady=(3, 8))
            self._update_settings_summary()
            self.set_status("输出目录已更新")

    def open_output(self):
        os.makedirs(self.output_dir, exist_ok=True)
        subprocess.Popen(["open", self.output_dir])

    def apple_music_auto_add_folder(self):
        """Legacy path retained only for display compatibility; importing no longer relies on it."""
        return None

    def toggle_auto_import(self):
        state = "开启" if self.auto_import.get() else "关闭"
        self._update_settings_summary()
        self.set_status(f"Apple Music 自动加入已{state}")


    def preview_split_tracks(self):
        indices = self.selected_indices()
        if len(indices) != 1:
            messagebox.showinfo("拆分预览", "请只选中一个合集音频，预览并编辑其曲目。")
            return
        idx = indices[0]
        item = self.items[idx]
        self.set_status(f"准备拆分预览：{P(item['source']).name}（可能需要分析静音）")
        def prepare():
            try:
                audio, info, tracks, mode = self._split_plan_for_item(item)
                # Use edited item-level album metadata as the authoritative values.
                info = dict(info)
                info['album'] = item['tags'].get('album') or info.get('album') or P(audio).stem
                info['artist'] = item['tags'].get('albumartist') or item['tags'].get('artist') or info.get('artist', '')
                for track in tracks:
                    if not track.get('artist'):
                        track['artist'] = item['tags'].get('artist') or info['artist']
                self.root.after(0, lambda: self._show_split_preview(idx, audio, info, tracks, mode))
            except Exception as exc:
                self.root.after(0, lambda e=exc: messagebox.showerror("拆分预览失败", str(e)))
                self.set_status("拆分预览失败")
        threading.Thread(target=prepare, daemon=True).start()

    def _show_split_preview(self, idx, audio, info, tracks, mode):
        win = tk.Toplevel(self.root)
        self._prepare_popup(win, "拆分曲目预览", "860x500")
        body = self._popup_body(win, 12)
        ttk.Label(body, text=f"{info.get('album','')}  ·  {len(tracks)} 首", style="PopupMuted.TLabel").pack(anchor="w", pady=(0, 8))
        cols=('number','title','artist','start','end','duration')
        table=ttk.Treeview(body,columns=cols,show='headings',selectmode='browse')
        labels={'number':'曲序','title':'歌曲名称','artist':'歌手','start':'开始时间','end':'结束时间','duration':'时长'}
        widths={'number':65,'title':300,'artist':180,'start':100,'end':100,'duration':90}
        for c in cols:
            table.heading(c,text=labels[c]); table.column(c,width=widths[c],anchor='w')
        table.pack(fill='both',expand=True)
        def timefmt(v):
            v=float(v); m=int(v//60); sec=v-m*60
            return f"{m:02d}:{sec:05.2f}"
        def refill():
            table.delete(*table.get_children())
            for i,t in enumerate(tracks):
                table.insert('', 'end', iid=str(i), values=(t.get('track',i+1),t.get('title',''),t.get('artist',''),timefmt(t['start']),timefmt(t['end']),timefmt(t['end']-t['start'])))
        refill()
        ttk.Label(body,text="双击曲目或时间可编辑",style="PopupMuted.TLabel").pack(anchor="w", pady=(6, 0))
        def edit_cell(event):
            row=table.identify_row(event.y); col=table.identify_column(event.x)
            if not row or col not in ('#1','#2','#3','#4','#5'): return
            ix=int(row); field={'#1':'track','#2':'title','#3':'artist','#4':'start','#5':'end'}[col]
            old=tracks[ix].get(field,'')
            editor=tk.Toplevel(win); self._prepare_popup(editor, '编辑曲目信息', '380x170', parent=win)
            eb=self._popup_body(editor, 12)
            ttk.Label(eb,text={'track':'曲序','title':'歌曲名称','artist':'歌手','start':'开始时间','end':'结束时间'}[field],style="Popup.TLabel").pack(anchor="w",pady=(0,6))
            ent=ttk.Entry(eb,width=38); ent.pack(fill="x"); ent.insert(0,timefmt(old) if field in ('start','end') else str(old)); ent.focus_set()
            def save():
                value=ent.get().strip()
                if field=='track':
                    try: tracks[ix][field]=int(value)
                    except ValueError: messagebox.showerror('曲序无效','曲序必须是数字。',parent=editor); return
                elif field in ('start','end'):
                    try: new_time=parse_time_value(value)
                    except Exception as exc: messagebox.showerror('时间格式错误',str(exc),parent=editor); return
                    old_times=[(float(t['start']),float(t['end'])) for t in tracks]
                    if field=='start':
                        tracks[ix]['start']=new_time
                        if ix>0: tracks[ix-1]['end']=new_time
                    else:
                        tracks[ix]['end']=new_time
                        if ix+1<len(tracks): tracks[ix+1]['start']=new_time
                    invalid=any(t['start']<0 or t['end']<=t['start'] for t in tracks)
                    if invalid:
                        for t,(st,en) in zip(tracks,old_times): t['start'],t['end']=st,en
                        messagebox.showerror('时间边界无效','调整后存在负数或结束时间不晚于开始时间。',parent=editor); return
                else: tracks[ix][field]=value
                refill(); editor.destroy()
            ttk.Button(eb,text='保存',command=save,style="PopupAccent.TButton").pack(anchor="e",pady=(10,0))
        table.bind('<Double-1>',edit_cell)
        def apply_preview():
            self.split_plans[self.items[idx]['source']]={'audio':audio,'info':dict(info),'tracks':[dict(t) for t in tracks],'mode':mode}
            self.items[idx]['status']=f'已预览 {len(tracks)} 首'
            self.refresh(); win.destroy()
            self.set_status(f"拆分曲目已确认：{len(tracks)} 首；开始工作时将使用此预览")
        row=ttk.Frame(body,style="Popup.TFrame"); row.pack(fill="x",pady=(12,0))
        ttk.Button(row,text='取消',command=win.destroy,style="Popup.TButton").pack(side='right',padx=(6,0))
        ttk.Button(row,text='确认',command=apply_preview,style="PopupAccent.TButton").pack(side='right')

    def import_into_apple_music(self, files):
        """Import generated files directly into Music.app through AppleScript."""
        imported=[]; failures=[]
        # Use argv so filenames are not interpolated into AppleScript source.
        script='on run argv\n  tell application "Music" to add (POSIX file (item 1 of argv))\nend run'
        for file_path in files:
            if self.cancel_requested.is_set(): break
            try:
                proc=self._run_cancellable_process(['osascript','-e',script,file_path], timeout=60)
                if proc.returncode==0: imported.append(file_path)
                else: failures.append(f"{P(file_path).name}: {proc.stderr.strip() or proc.stdout.strip()}")
            except InterruptedError:
                break
            except Exception as exc:
                failures.append(f"{P(file_path).name}: {exc}")
        return imported, failures

    def _split_plan_for_item(self, item):
        """Build a split plan from a sidecar CUE, or estimate boundaries via silence detection."""
        audio = item["source"]
        cue = self.cue_files.get(audio) or self.cue_files.get(str(P(audio).parent / P(audio).stem))
        if not cue:
            sidecar = P(audio).with_suffix(".cue")
            if sidecar.exists(): cue = str(sidecar)
        if cue and P(cue).exists():
            info = parse_cue_file(cue)
            source_names = {P(x).name.lower() for x in info.get("files", [])}
            selected_name = P(audio).name.lower()
            # CUE FILE references may point to a sibling audio source. If exactly one exists, use it.
            if source_names and selected_name not in source_names:
                found = [P(cue).parent / name for name in info.get("files", []) if (P(cue).parent / name).exists()]
                if len(found) == 1:
                    audio = str(found[0])
                else:
                    raise ValueError(f"CUE 引用的音频与列表文件不匹配：{P(audio).name}")
            tracks = [t for t in info["tracks"] if not t.get("file") or P(t["file"]).name.lower() == P(audio).name.lower()]
            if not tracks: raise ValueError("CUE 没有包含所选音频对应的曲目")
            duration = self._audio_duration(audio)
            for i, track in enumerate(tracks):
                track["end"] = tracks[i+1]["start"] if i+1 < len(tracks) else duration
            return audio, info, tracks, "CUE"
        # Without CUE, use silence detection as an estimate. The status remains visible in the main window.
        self.set_status(f"检测静音分段：{P(audio).name}")
        silence_cmd = [self.ffmpeg, "-hide_banner", "-i", audio, "-af", "silencedetect=noise=-35dB:d=1.5", "-f", "null", "-"]
        logger = logging.getLogger("MusicImporter")
        logger.info("SILENCE DETECTION START | source=%s | command=%s", audio, shlex.join(silence_cmd))
        silence_started = time.monotonic()
        proc = subprocess.run(silence_cmd, capture_output=True, text=True)
        logger.info("SILENCE DETECTION END | source=%s | exit=%s | elapsed=%.3f sec", audio, proc.returncode, time.monotonic() - silence_started)
        if proc.stderr:
            logger.info("SILENCE DETECTION STDERR (last 5000 chars):\n%s", proc.stderr[-5000:])
        log = proc.stderr
        duration = self._audio_duration(audio)
        silence_starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", log)]
        silence_ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", log)]
        boundaries = [0.0]
        for st in silence_starts:
            en = next((x for x in silence_ends if x >= st), st)
            midpoint = (st + en) / 2
            if 3 < midpoint < duration - 3: boundaries.append(midpoint)
        boundaries.append(duration); boundaries = sorted(set(round(x, 3) for x in boundaries))
        tracks = [{"track": i+1, "title": f"Track {i+1:02d}", "artist": item["tags"].get("artist", ""),
                  "start": boundaries[i], "end": boundaries[i+1]} for i in range(len(boundaries)-1)]
        return audio, {"album": item["tags"].get("album") or P(audio).stem, "artist": item["tags"].get("albumartist") or item["tags"].get("artist", "")}, tracks, "静音检测估算"

    def _audio_duration(self, audio):
        logger = logging.getLogger("MusicImporter")
        logger.info("AUDIO DURATION PROBE | source=%s | command=%s", audio, shlex.join([self.ffmpeg, "-i", audio]))
        started = time.monotonic()
        proc = subprocess.run([self.ffmpeg, "-i", audio], capture_output=True, text=True)
        logger.info("AUDIO DURATION PROBE END | source=%s | exit=%s | elapsed=%.3f sec", audio, proc.returncode, time.monotonic() - started)
        if proc.stderr:
            logger.info("AUDIO DURATION PROBE STDERR (last 4000 chars):\n%s", proc.stderr[-4000:])
        dm = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr)
        if not dm: raise RuntimeError("无法读取音频时长")
        h, m, sec = dm.groups(); return int(h)*3600 + int(m)*60 + float(sec)

    def _run_cancellable_process(self, cmd, timeout=None):
        """Run a child process while allowing the UI stop button to terminate it; log timing and diagnostics."""
        logger = logging.getLogger("MusicImporter")
        if self.cancel_requested.is_set():
            raise InterruptedError("任务已取消")
        logger.info("SUBPROCESS START | timeout=%s | command=%s", timeout, shlex.join([str(x) for x in cmd]))
        started = time.monotonic()
        wall_started = time.time()
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.current_process = proc
        try:
            while proc.poll() is None:
                if self.cancel_requested.is_set():
                    proc.terminate()
                    try: proc.wait(timeout=3)
                    except subprocess.TimeoutExpired: proc.kill(); proc.wait()
                    out, err = proc.communicate()
                    raise InterruptedError("任务已取消")
                if timeout is not None and __import__("time").monotonic() - started > timeout:
                    proc.terminate()
                    try: proc.wait(timeout=3)
                    except subprocess.TimeoutExpired: proc.kill(); proc.wait()
                    out, err = proc.communicate()
                    raise TimeoutError(f"子进程超过 {timeout} 秒，已终止")
                try:
                    out, err = proc.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
            else:
                out, err = proc.communicate()
            elapsed = time.monotonic() - started
            logger.info("SUBPROCESS END | exit_code=%s | elapsed=%.3f sec | started_epoch=%.3f", proc.returncode, elapsed, wall_started)
            if err:
                logger.info("SUBPROCESS STDERR (last 12000 chars):\n%s", err[-12000:])
            if out:
                logger.info("SUBPROCESS STDOUT (last 3000 chars):\n%s", out[-3000:])
            if self.cancel_requested.is_set():
                raise InterruptedError("任务已取消")
            return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
        except Exception:
            logger.exception("SUBPROCESS FAILED | command=%s", shlex.join([str(x) for x in cmd]))
            raise
        finally:
            self.current_process = None

    def stop_work(self):
        if not self.busy:
            return
        self.cancel_requested.set()
        self.set_status("正在停止任务…")
        self.stop_button.configure(state="disabled")
        proc = self.current_process
        if proc and proc.poll() is None:
            try: proc.terminate()
            except Exception: pass

    def on_close(self):
        if self.busy:
            if not messagebox.askyesno("任务仍在运行", "任务正在运行。停止任务并退出吗？"):
                return
            self.closing_requested = True
            self.cancel_requested.set()
            proc = self.current_process
            if proc and proc.poll() is None:
                try: proc.terminate()
                except Exception: pass
            self.set_status("正在停止任务并退出…")
            # The worker will finish cleanup and close the window on its own thread-safe callback.
            return
        self.root.destroy()

    def start_work(self, action="convert"):
        if self.busy: return
        if not self.items:
            messagebox.showinfo("提示", "请先添加音频文件。")
            return
        selected = self.selected_indices()
        if not selected:
            messagebox.showinfo("提示", "请先在列表中选中要处理的音频文件。")
            return
        if not self.ffmpeg:
            messagebox.showerror("缺少 FFmpeg", "请先安装 FFmpeg：brew install ffmpeg")
            return
        split_enabled = action in {"split", "split_convert"}
        convert_enabled = action in {"convert", "split_convert"}
        # Preview/edit is optional. If the user has not confirmed a plan, the worker
        # will auto-detect boundaries from CUE or silence detection. Silence detection
        # is approximate, so preview remains available as a correction step.
        fmt = self.output_format.get()
        if split_enabled and not convert_enabled:
            fallback = [P(self.items[i]['source']).name for i in selected if P(self.items[i]['source']).suffix.lower() in {'.ape', '.dsf', '.dff'}]
            if fallback and not messagebox.askyesno("部分源格式无法直接拆分", "以下源格式在常见 FFmpeg 构建中没有对应输出封装，拆分时会转为 FLAC：\n\n" + "\n".join(fallback[:8]) + "\n\n是否继续？"):
                return
        if split_enabled and not convert_enabled:
            # Keep source extension and codec when splitting without format conversion.
            pass
        elif not split_enabled:
            lossy = {".mp3", ".aac", ".ogg", ".opus"}
            lossless = {"ALAC", "FLAC", "WAV", "AIFF"}
            if any(P(self.items[i]["source"]).suffix.lower() in lossy for i in selected) and fmt in lossless:
                if not messagebox.askyesno("有损转无损", "有损源转换为无损格式不会恢复音质，仍要继续？"): return
        self.busy = True
        self.cancel_requested.clear()
        for button in self.action_buttons:
            button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress.configure(maximum=len(selected), value=0)
        self.set_status("任务已启动…")
        threading.Thread(target=self.work_worker, args=(selected, split_enabled, convert_enabled, fmt), daemon=True).start()

    def work_worker(self, selected, split_enabled, convert_enabled, fmt):
        successes = 0; failures = []; generated_files = []
        for n, idx in enumerate(selected, start=1):
            if self.cancel_requested.is_set(): break
            item = self.items[idx]
            src = item["source"]
            self.root.after(0, lambda i=idx: self.items[i].update(status="处理中"))
            self.root.after(0, self.refresh)
            embedded_temp_cover = None
            try:
                if split_enabled:
                    task_generated = []
                    plan = self.split_plans.get(src)
                    if not plan:
                        self.set_status(f"自动分析拆分边界：{P(src).name}（可提前预览/编辑以修正曲目）")
                        audio, info, tracks, mode = self._split_plan_for_item(item)
                        info = dict(info)
                        info['album'] = item['tags'].get('album') or info.get('album') or P(audio).stem
                        info['artist'] = item['tags'].get('albumartist') or item['tags'].get('artist') or info.get('artist', '')
                        for track in tracks:
                            if not track.get('artist'):
                                track['artist'] = item['tags'].get('artist') or info['artist']
                        plan = {'audio': audio, 'info': info, 'tracks': tracks, 'mode': mode}
                        self.split_plans[src] = plan
                    audio, info, tracks, mode = plan['audio'], plan['info'], plan['tracks'], plan['mode']
                    output = P(self.output_dir)
                    output.mkdir(parents=True, exist_ok=True)
                    if convert_enabled:
                        ext, codec = self._format_spec(fmt)
                    else:
                        (ext, codec), fallback_flac = self._split_source_spec(src)
                        # For APE/DSF/DFF, FLAC is the practical output because FFmpeg's
                        # commonly available muxers cannot write the original container.
                        if fallback_flac:
                            self.set_status(f"{P(src).suffix.upper()} 不支持直接分段输出，改为 FLAC：{P(src).name}")
                    album = safe_component(item["tags"].get("album") or info.get("album") or P(audio).stem, P(audio).stem)
                    album_artist = item["tags"].get("albumartist") or item["tags"].get("artist") or info.get("artist", "")
                    cover = item.get("artwork")
                    embedded_temp_cover = None
                    if not cover:
                        embedded_temp_cover = self.extract_embedded_artwork(audio)
                        cover = embedded_temp_cover
                    date = item["tags"].get("date", "")
                    # v14: segment the complete album in one FFmpeg invocation.
                    # Stage all files outside the final output folder so cancellation/failure
                    # never leaves partial tracks where Apple Music can auto-import them.
                    import tempfile
                    first_start = float(tracks[0]["start"])
                    last_end = float(tracks[-1]["end"])
                    if first_start < 0 or last_end <= first_start:
                        raise ValueError("拆分时间范围无效")
                    for j, track in enumerate(tracks, start=1):
                        start_t, end_t = float(track["start"]), float(track["end"])
                        if start_t < first_start or end_t <= start_t or end_t > last_end + 0.01:
                            raise ValueError(f"第 {j} 首时间段无效")
                    # Segment timestamps are relative to the selected first-track start.
                    boundaries = [float(t["end"]) - first_start for t in tracks[:-1]]
                    boundaries = [b for b in boundaries if b > 0]
                    with tempfile.TemporaryDirectory(prefix="musicimporter_stage_") as stage_dir:
                        pattern = str(P(stage_dir) / f"segment_%03d.{ext}")
                        muxer = {"m4a":"mp4", "aiff":"aiff", "wav":"wav", "flac":"flac", "mp3":"mp3", "ogg":"ogg", "opus":"opus", "wv":"wv"}.get(ext, ext)
                        cmd = [self.ffmpeg, "-nostdin", "-y", "-ss", f"{first_start:.6f}", "-i", audio,
                               "-map", "0:a:0", *codec, "-f", "segment", "-segment_start_number", "0",
                               "-reset_timestamps", "1", "-segment_format", muxer]
                        if boundaries:
                            cmd += ["-segment_times", ",".join(f"{b:.6f}" for b in boundaries)]
                        cmd += [pattern]
                        self.set_status(f"批量拆分 {P(src).name} · {len(tracks)} 首（单次 FFmpeg）")
                        pr = self._run_cancellable_process(cmd, timeout=6*60*60)
                        if pr.returncode != 0:
                            raise RuntimeError(f"批量分段失败（FFmpeg）：\n{pr.stderr[-1500:]}")
                        staged = sorted(P(stage_dir).glob(f"segment_*.{ext}"))
                        if len(staged) != len(tracks):
                            raise RuntimeError(f"批量分段结果数量不匹配：预期 {len(tracks)} 首，实际 {len(staged)} 首。\n{pr.stderr[-800:]}")
                        # Write tags and artwork to staged tracks, then move into final
                        # names only after every segment has been produced successfully.
                        final_paths = []
                        for j, (track, staged_file) in enumerate(zip(tracks, staged), start=1):
                            self.set_status(f"整理标签与封面 {j}/{len(tracks)}：{track.get('title', '')}")
                            title = str(track.get("title") or f"Track {j:02d}")
                            number = int(track.get("track", j))
                            artist = track.get("artist") or item["tags"].get("artist") or album_artist
                            self._write_track_metadata(staged_file, title, artist, album, album_artist,
                                number, date, item["tags"].get("genre", ""), item["tags"].get("discnumber", ""), cover)
                            dest = output / f"{number:02d} - {safe_component(title, f'Track {j:02d}')}.{ext}"
                            if dest.exists():
                                counter = 2
                                base = dest.stem
                                while (output / f"{base} ({counter}).{ext}").exists(): counter += 1
                                dest = output / f"{base} ({counter}).{ext}"
                            final_paths.append((staged_file, dest))
                        # Commit staged outputs. If cancellation arrives before commit,
                        # no new final files are published.
                        if self.cancel_requested.is_set():
                            raise InterruptedError("任务已取消")
                        for staged_file, dest in final_paths:
                            if self.cancel_requested.is_set():
                                raise InterruptedError("任务已取消")
                            staged_file.replace(dest)
                            task_generated.append(str(dest))
                    generated_files.extend(task_generated)
                    self.root.after(0, lambda i=idx: self.items[i].update(status=f"拆分完成 ({mode})"))
                else:
                    # Standard format conversion: reuse existing conversion worker on selected items only.
                    self.convert_single_item(item, fmt)
                    if item.get('output'):
                        generated_files.append(item['output'])
                    self.root.after(0, lambda i=idx: self.items[i].update(status="转换完成"))
                successes += 1
            except InterruptedError:
                self.root.after(0, lambda i=idx: self.items[i].update(status="已取消"))
                self.root.after(0, self.refresh)
                break
            except Exception as exc:
                failures.append(f"{P(src).name}: {exc}")
                self.root.after(0, lambda i=idx: self.items[i].update(status="失败"))
            finally:
                if embedded_temp_cover:
                    try: P(embedded_temp_cover).unlink()
                    except OSError: pass
            self.root.after(0, self.refresh)
            self.root.after(0, lambda v=n: self.progress.configure(value=v))
            self.set_status(f"任务进度 {n}/{len(selected)} · 成功 {successes} · 失败 {len(failures)}")
        if not self.cancel_requested.is_set():
            self.root.after(0, lambda: self.progress.configure(value=len(selected)))
        import_summary = ""
        if self.auto_import.get() and generated_files and not self.cancel_requested.is_set():
            self.set_status("正在导入 Apple Music…")
            imported, import_failures = self.import_into_apple_music(generated_files)
            import_summary = f"\nApple Music 导入成功：{len(imported)} 首；导入失败：{len(import_failures)} 首"
            if import_failures:
                import_summary += "\n导入错误：\n" + "\n".join(import_failures[:5])
        cancelled = self.cancel_requested.is_set()
        self.busy=False
        # Generated files stay in the output directory and are not added to the
        # main source list. Lyrics matched on the source are explicitly copied by
        # convert_single_item() into converted outputs, so there is no need to
        # load those outputs back into the UI.
        def finish_work_ui():
            for button in self.action_buttons:
                button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            msg=("任务已取消" if cancelled else "任务完成") + f"\n完成 {successes} 个任务 · 失败 {len(failures)}"
            if generated_files and not cancelled:
                msg += f"\n输出文件已保存到设置的目录。原文件匹配的歌词会随格式转换写入输出文件。"
            msg += import_summary
            if failures: msg += "\n\n" + "\n".join(failures[:5])
            if not self.closing_requested:
                messagebox.showinfo("任务结果", msg)
            self.status.config(text=("任务已取消" if cancelled else "任务完成") + f"：成功 {successes} · 失败 {len(failures)}")
            if self.closing_requested:
                self.root.destroy()
        self.root.after(0, finish_work_ui)

    def _format_spec(self, fmt):
        return {"ALAC":("m4a",["-c:a","alac"]),"FLAC":("flac",["-c:a","flac"]),
            "WAV":("wav",["-c:a","pcm_s24le"]),"AIFF":("aiff",["-c:a","pcm_s24be"]),
            "MP3":("mp3",["-c:a","libmp3lame","-b:a","320k"]),"AAC":("m4a",["-c:a","aac","-b:a","256k"]),
            "Opus":("opus",["-c:a","libopus","-b:a","192k"])}[fmt]

    def _source_format_spec(self, src):
        ext=P(src).suffix.lower()
        return {".flac":("flac",["-c:a","flac"]),".wav":("wav",["-c:a","pcm_s24le"]),
            ".ape":("ape",["-c:a","ape"]),".aiff":("aiff",["-c:a","pcm_s24be"]),".aif":("aiff",["-c:a","pcm_s24be"]),
            ".mp3":("mp3",["-c:a","libmp3lame","-b:a","320k"]),".m4a":("m4a",["-c:a","aac","-b:a","256k"]),
            ".aac":("m4a",["-c:a","aac","-b:a","256k"]),".ogg":("ogg",["-c:a","libvorbis"]),".opus":("opus",["-c:a","libopus","-b:a","192k"]),
            ".wv":("wv",["-c:a","wavpack"]),".dsf":("dsf",["-c:a","pcm_s24le"]),".dff":("dff",["-c:a","pcm_s24le"])}.get(ext, (ext.lstrip("."),["-c:a","flac"]))

    def _split_source_spec(self, src):
        """Choose a stream-copy container when FFmpeg can mux it; otherwise fallback to FLAC."""
        ext = P(src).suffix.lower()
        copy_specs = {
            ".flac": ("flac", ["-c:a", "copy"]),
            ".wav": ("wav", ["-c:a", "copy"]),
            ".aiff": ("aiff", ["-c:a", "copy"]),
            ".aif": ("aiff", ["-c:a", "copy"]),
            ".mp3": ("mp3", ["-c:a", "copy"]),
            ".m4a": ("m4a", ["-c:a", "copy"]),
            ".aac": ("m4a", ["-c:a", "copy"]),
            ".ogg": ("ogg", ["-c:a", "copy"]),
            ".opus": ("opus", ["-c:a", "copy"]),
            ".wv": ("wv", ["-c:a", "copy"]),
        }
        if ext in copy_specs:
            return copy_specs[ext], False
        # FFmpeg can decode these source formats, but it does not provide a matching
        # output muxer in common builds. Split them into FLAC rather than failing.
        return ("flac", ["-c:a", "flac"]), True

    def match_selected_lyrics(self):
        """Online-match lyrics for selected tracks and write matches directly into their tags."""
        if self.busy:
            messagebox.showinfo("任务进行中", "请等当前转换任务结束后再匹配歌词。")
            return
        selected = self.selected_indices()
        if not selected:
            messagebox.showinfo("提示", "请先在主界面选中要匹配歌词的歌曲。")
            return
        targets = [self.items[i]["source"] for i in selected]
        self.busy = True
        for button in self.action_buttons:
            button.configure(state="disabled")
        self.set_status(f"开始在线匹配歌词：{len(targets)} 首…")
        logger = logging.getLogger("MusicImporter")
        logger.info("LYRICS MATCH BATCH START | count=%d", len(targets))

        def worker():
            matched = 0
            failed = []
            details = []
            for n, target in enumerate(targets, 1):
                self.root.after(0, lambda i=n, t=target: self.set_status(f"在线匹配歌词 {i}/{len(targets)}：{P(t).name}"))
                try:
                    ok, detail = online_match_lyrics(target)
                    if ok:
                        matched += 1
                    else:
                        failed.append(P(target).name)
                    details.append(f"{P(target).name}：{detail}")
                except Exception as exc:
                    failed.append(P(target).name)
                    details.append(f"{P(target).name}：匹配失败 - {exc}")
                    logger.exception("LYRICS MATCH ITEM ERROR | target=%s", target)
            summary = f"在线歌词匹配完成：成功 {matched}/{len(targets)} 首；未匹配/失败 {len(failed)} 首。"
            report = summary + "\n\n" + "\n".join(details)
            logger.info("LYRICS MATCH BATCH END | success=%d | failed=%d", matched, len(failed))
            def finish():
                self.busy = False
                for button in self.action_buttons:
                    button.configure(state="normal")
                self.set_status(summary)
                messagebox.showinfo("在线匹配歌词", report)
            self.root.after(0, finish)
        threading.Thread(target=worker, daemon=True, name="LyricsOnlineMatch").start()

    def _write_track_metadata(self, path, title, artist, album, album_artist, track, date, genre, disc, cover):
        """Best-effort cross-format tags and artwork using Mutagen after batch splitting."""
        logger = logging.getLogger("MusicImporter")
        started = time.monotonic()
        logger.info("METADATA/ARTWORK START | file=%s | title=%s | cover=%s", path, title, cover)
        log_file_info("STAGED TRACK BEFORE TAGGING", path)
        if cover:
            log_file_info("METADATA COVER", cover)
        from mutagen import File as MutagenFile
        audio = MutagenFile(str(path))
        if audio is None:
            raise RuntimeError(f"无法写入输出文件标签：{P(path).name}")
        if audio.tags is None:
            try: audio.add_tags()
            except Exception: pass
        tags = audio.tags
        values = {"title": title, "artist": artist, "album": album, "albumartist": album_artist,
                  "date": date, "genre": genre, "tracknumber": str(track), "discnumber": str(disc)}
        # Easy tags cover the common FLAC/MP3/MP4/Ogg/Opus/AIFF cases.
        try:
            easy = MutagenFile(str(path), easy=True)
            if easy is not None and easy.tags is not None:
                for key, value in values.items():
                    if value not in (None, ""):
                        try: easy[key] = [str(value)]
                        except Exception: pass
                easy.save()
        except Exception:
            pass
        if cover and os.path.isfile(cover):
            ext = P(path).suffix.lower()
            try:
                from mutagen.flac import FLAC, Picture
                if ext == ".flac":
                    f = FLAC(path); pic = Picture(); pic.type = 3; pic.mime = "image/jpeg" if P(cover).suffix.lower() in {".jpg", ".jpeg"} else "image/png"; pic.desc = "Cover"; pic.data = P(cover).read_bytes(); f.clear_pictures(); f.add_picture(pic); f.save()
                elif ext == ".mp3":
                    from mutagen.id3 import ID3, APIC, error
                    m = ID3(path); m.delall("APIC"); m.add(APIC(encoding=3, mime="image/jpeg" if P(cover).suffix.lower() in {".jpg", ".jpeg"} else "image/png", type=3, desc="Cover", data=P(cover).read_bytes())); m.save()
                elif ext in {".aiff", ".aif"}:
                    from mutagen.id3 import ID3, APIC
                    m = ID3(path); m.delall("APIC"); m.add(APIC(encoding=3, mime="image/jpeg" if P(cover).suffix.lower() in {".jpg", ".jpeg"} else "image/png", type=3, desc="Cover", data=P(cover).read_bytes())); m.save()
                elif ext in {".m4a", ".aac"}:
                    from mutagen.mp4 import MP4, MP4Cover
                    m = MP4(path); fmt = MP4Cover.FORMAT_JPEG if P(cover).suffix.lower() in {".jpg", ".jpeg"} else MP4Cover.FORMAT_PNG; m.tags["covr"] = [MP4Cover(P(cover).read_bytes(), imageformat=fmt)]; m.save()
            except Exception as e:
                # Cover embedding is non-fatal for formats where Mutagen lacks support.
                logger.exception("METADATA COVER EMBEDDING FAILED | file=%s | cover=%s", path, cover)
                self.set_status(f"封面写入未完成：{P(path).name}（{e}）")
        logger.info("METADATA/ARTWORK END | file=%s | elapsed=%.3f sec", path, time.monotonic() - started)
        log_file_info("STAGED TRACK AFTER TAGGING", path)

    def _embed_conversion_artwork(self, output_path, cover_path):
        """Embed artwork; normalize uncommon image formats to JPEG for compatibility."""
        ext = P(output_path).suffix.lower()
        cover = P(cover_path)
        image_ext = cover.suffix.lower()
        temp_jpeg = None
        if image_ext not in {".jpg", ".jpeg", ".png"}:
            temp_jpeg = P(output_path).with_name(P(output_path).stem + ".cover-normalized-temp.jpg")
            proc = subprocess.run([self.ffmpeg, "-nostdin", "-y", "-i", str(cover), "-frames:v", "1", str(temp_jpeg)],
                                  capture_output=True, text=True, timeout=60)
            if proc.returncode != 0 or not temp_jpeg.exists() or not temp_jpeg.stat().st_size:
                raise RuntimeError("封面图片格式无法转换为兼容格式：" + (proc.stderr[-1000:] if proc.stderr else "未知错误"))
            cover = temp_jpeg
            image_ext = ".jpg"
        data = cover.read_bytes()
        mime = "image/jpeg" if image_ext in {".jpg", ".jpeg"} else "image/png"
        try:
            if ext == ".flac":
                from mutagen.flac import FLAC, Picture
                audio = FLAC(output_path)
                picture = Picture(); picture.type = 3; picture.mime = mime; picture.desc = "Cover"; picture.data = data
                audio.clear_pictures(); audio.add_picture(picture); audio.save(); return
            if ext in {".mp3", ".aiff", ".aif"}:
                from mutagen.id3 import ID3, APIC, ID3NoHeaderError
                try: audio = ID3(output_path)
                except ID3NoHeaderError: audio = ID3()
                audio.delall("APIC"); audio.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
                audio.save(output_path); return
            if ext == ".m4a":
                from mutagen.mp4 import MP4, MP4Cover
                audio = MP4(output_path)
                image_format = MP4Cover.FORMAT_JPEG if image_ext in {".jpg", ".jpeg"} else MP4Cover.FORMAT_PNG
                audio.tags["covr"] = [MP4Cover(data, imageformat=image_format)]; audio.save(); return
            if ext in {".wav", ".wave"}:
                from mutagen.wave import WAVE
                from mutagen.id3 import APIC
                audio = WAVE(output_path)
                if audio.tags is None: audio.add_tags()
                audio.tags.delall("APIC"); audio.tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=data))
                audio.save(); return
            if ext in {".opus", ".ogg"}:
                from mutagen.oggopus import OggOpus
                from mutagen.oggvorbis import OggVorbis
                from mutagen.flac import Picture
                audio = OggOpus(output_path) if ext == ".opus" else OggVorbis(output_path)
                picture = Picture(); picture.type = 3; picture.mime = mime; picture.desc = "Cover"; picture.data = data
                audio["metadata_block_picture"] = [picture.write().hex()]; audio.save(); return
            if ext == ".wv":
                from mutagen import File as MutagenFile
                from mutagen.flac import Picture
                audio = MutagenFile(output_path)
                if audio is None: raise RuntimeError("无法打开 WavPack 输出文件写入封面")
                picture = Picture(); picture.type = 3; picture.mime = mime; picture.desc = "Cover"; picture.data = data
                audio["metadata_block_picture"] = [picture.write().hex()]; audio.save(); return
            raise RuntimeError(f"当前输出格式不支持封面写入：{ext}")
        finally:
            if temp_jpeg:
                try: temp_jpeg.unlink()
                except OSError: pass

    def convert_single_item(self, item, fmt):
        src=item["source"]; tags=item["tags"]
        logger = logging.getLogger("MusicImporter")
        logger.info("CONVERSION REQUEST | format=%s | source=%s | tags=%s", fmt, src, tags)
        log_file_info("SOURCE", src)
        ext,codec=self._format_spec(fmt)
        out_dir=P(self.output_dir)/safe_component(tags.get("album"),"Unknown Album"); out_dir.mkdir(parents=True,exist_ok=True)
        out=out_dir/(safe_component(tags.get("title"),P(src).stem)+"."+ext)
        if out.exists():
            suffix=2
            while (out_dir/(safe_component(tags.get("title"),P(src).stem)+f" ({suffix})."+ext)).exists(): suffix+=1
            out=out_dir/(safe_component(tags.get("title"),P(src).stem)+f" ({suffix})."+ext)
        art=item.get("artwork")
        embedded_temp_cover = None
        if not art:
            embedded_temp_cover = self.extract_embedded_artwork(src)
            art = embedded_temp_cover
        embed_cover=bool(art and os.path.isfile(art) and ext in {"m4a", "flac", "mp3", "aiff", "wav", "opus", "ogg", "wv"})
        logger.info("CONVERSION OPTIONS | target=%s | output=%s | output_format=%s | codec=%s | cover=%s | cover_embedded_after_audio_conversion=%s", fmt, out, ext, codec, art, embed_cover)
        if embed_cover:
            log_file_info("COVER INPUT", art)
        # Convert audio only. Do NOT feed the artwork to FFmpeg as a looping video
        # stream: that path encodes thousands of MJPEG frames and can make a song
        # conversion take nearly its full playback duration.
        cmd=[self.ffmpeg,"-nostdin","-y","-i",src,"-map","0:a:0",*codec,"-map_metadata","0"]
        for key,val in [("title","title"),("artist","artist"),("album","album"),("album_artist","albumartist"),("date","date"),("genre","genre"),("track","tracknumber"),("disc","discnumber")]:
            cmd += ["-metadata",f"{key}={tags.get(val,'')}"]
        cmd.append(str(out))
        # Lyrics are handled separately from FFmpeg metadata mapping because
        # cross-container lyric-tag mapping can omit or alter text.
        lyrics = read_lyrics(src)
        logger.info("LYRICS SOURCE | source=%s | present=%s | chars=%d", src, bool(lyrics), len(lyrics))
        conversion_started = time.monotonic()
        try:
            pr=self._run_cancellable_process(cmd, timeout=6*60*60)
        except InterruptedError:
            logger.warning("CONVERSION CANCELLED | source=%s | elapsed=%.3f sec", src, time.monotonic() - conversion_started)
            try:
                if out.exists(): out.unlink()
            except OSError: pass
            if embedded_temp_cover:
                try: P(embedded_temp_cover).unlink()
                except OSError: pass
            raise
        if pr.returncode != 0 or not out.exists() or not out.stat().st_size:
            logger.error("CONVERSION INVALID OUTPUT | source=%s | exit_code=%s | output_exists=%s", src, pr.returncode, out.exists())
            try:
                if out.exists(): out.unlink()
            except OSError: pass
            if embedded_temp_cover:
                try: P(embedded_temp_cover).unlink()
                except OSError: pass
            if self.cancel_requested.is_set(): raise InterruptedError("任务已取消")
            raise RuntimeError(pr.stderr[-600:])
        # Embed artwork with Mutagen after audio conversion. This writes a single
        # static cover image to the container/tag, rather than encoding image
        # frames alongside the entire audio stream.
        if embed_cover:
            try:
                self._embed_conversion_artwork(str(out), art)
                logger.info("CONVERSION COVER EMBEDDED | output=%s | cover=%s", out, art)
            except Exception:
                logger.exception("CONVERSION COVER EMBEDDING FAILED | output=%s | cover=%s", out, art)
                self.set_status(f"封面写入未完成：{P(out).name}（音频转换已完成）")
        # Restore lyrics explicitly in the destination's native tag after FFmpeg.
        # A lyric-write failure is logged but does not discard the converted audio.
        if lyrics:
            try:
                wrote = write_lyrics(str(out), lyrics)
                logger.info("LYRICS WRITE | output=%s | success=%s | chars=%d", out, wrote, len(lyrics))
            except Exception:
                logger.exception("LYRICS WRITE FAILED | output=%s", out)
        if embedded_temp_cover:
            try: P(embedded_temp_cover).unlink()
            except OSError: pass
        item["output"]=str(out)
        logger.info("CONVERSION SUCCESS | source=%s | output=%s | total_elapsed=%.3f sec", src, out, time.monotonic() - conversion_started)
        log_file_info("OUTPUT", out)



def main():
    root = tk.Tk()
    app = MusicImporter(root)
    root.mainloop()


if __name__ == "__main__":
    main()
