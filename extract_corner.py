"""전사된 방송에서 특정 코너만 잘라낸다 (오디오 + 전사).

현재 지원 코너: Screen English (KBS 굿모닝 팝스)

- 입력: 전사된 폴더 또는 개별 mp3. .srt가 있어야 한다.
- 자동 검출: 코너 끝 멘트(예: "Screen English ... 여기까지")를 정규식으로 찾고,
  시작은 인트로 이후 첫 본문 구간으로 잡는다.
- 수동 지정: --start / --end (MM:SS 또는 HH:MM:SS) 로 강제 지정 가능.
- 출력: <out-dir>/<date>_<corner>.mp3 / .txt / .srt / .md
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from ngram_detector import NgramDetector

# Windows cp949 콘솔에서도 한글/특수문자 출력이 깨지지 않도록 강제 UTF-8.
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".flac"}
DATE_PREFIX_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
SRT_TIME_RE = re.compile(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})")


@dataclass
class Segment:
    index: int
    start: float
    end: float
    text: str


CLOSING_TAIL_RE = re.compile(
    r"여기까지|마무리|see\s*you|만나(?!세요)|만날\s*게요|(?<!\w)볼게요|Bye",
    re.IGNORECASE,
)


CORNER_DEFS = {
    "screen_english": {
        "label": "Screen English",
        # === INTRO 마커 (진짜 코너 시작점 감지용) ===
        "intro_patterns": [
            re.compile(r"스크린\s*잉글리[쉬시]|Screen\s*English", re.IGNORECASE),
        ],
        "welcome_patterns": [
            re.compile(
                r"Chris|크리스|good\s*morning|굿모닝|welcome|웰컴|theater|시어터|타임|time",
                re.IGNORECASE,
            ),
        ],
        # === START 마커 ===
        # 진행자가 영화 장면을 처음 들려주기 직전에 쓰는 정형구.
        # "오늘 준비된 장면 듣고 오겠습니다" 류. 이 구절이 등장하는 segment의 시작을 코너 시작으로.
        "start_patterns": [
            re.compile(
                r"오늘[^.\n]{0,20}장면[^.\n]{0,20}(?:듣고\s*[오올와온]|들어\s*[보봐])",
                re.IGNORECASE | re.DOTALL,
            ),
            re.compile(
                r"준비[된한]\s*장면[^.\n]{0,20}(?:듣고\s*[오올와온]|들어\s*[보봐])",
                re.IGNORECASE | re.DOTALL,
            ),
            re.compile(
                r"장면\s*(?:듣고\s*[오올와온]|들어\s*[보봐]겠|한\s*번\s*들어)",
                re.IGNORECASE | re.DOTALL,
            ),
            re.compile(
                r"오늘[^.\n]{0,20}들으실\s*장면",
                re.IGNORECASE | re.DOTALL,
            ),
        ],
        # === END 마커 ===
        # 본 코너 끝을 알리는 마커. 코너명 + "여기까지/마무리" 조합으로
        # 다른 코너(스마트 위리 잉글리쉬 등)의 끝 멘트와 구분한다.
        "end_patterns": [
            re.compile(
                r"(?:스크린\s*잉글리[쉬시]|Screen\s*English)"
                r".{0,150}?(?:여기까지|마무리)",
                re.IGNORECASE | re.DOTALL,
            ),
            # 보조: Chris/크리스 게스트와의 작별 인사 (코너 종료 정형구)
            re.compile(
                r"(?:Chris|크리스).{0,80}?"
                r"(?:see\s*you|I'?ll\s*see\s*you|Bye|만나|만날\s*게요|(?<!\w)볼게요)",
                re.IGNORECASE | re.DOTALL,
            ),
            re.compile(
                r"(?:see\s*you|I'?ll\s*see\s*you).{0,30}?"
                r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|tomorrow|next\s*week)",
                re.IGNORECASE | re.DOTALL,
            ),
        ],
        # === 검출 파라미터 ===
        "start_max_minutes": 12,         # START 마커 탐색 상한 (12분 이내)
        "start_window_segments": 4,      # START 마커 슬라이딩 윈도우 크기
        "max_end_minutes": 22,           # END 마커 탐색 상한
        "end_window_segments": 4,        # END 마커 슬라이딩 윈도우 크기
        "intro_skip_seconds": 60.0,      # START 마커 없을 때 폴백: 이 시간 이후 첫 segment
        "max_duration_minutes": 20,      # 폴백 start 추정용 안전 상한
    },
    "review_time_screen": {
        "label": "Review Time (Screen English)",
        "start_patterns": [
            r"스크린\s*잉글리시\s*복습",
            r"복습해\s*볼까요",
        ],
        "end_patterns": [
            r"팝스\s*잉글리시로\s*넘어갑니다",
            r"팝스\s*잉글리시\s*복습",
            r"다음은\s*팝스\s*잉글리시",
        ],
        "max_start_minutes": 15,
        "max_end_minutes": 40,
        "fallback_start": 60,  # 오프닝 1분 스킵
        "fallback_end": 1200,  # 20분
    }
}


# ──────────────────────────────────────────────
# N-gram 감지기 lazy singleton
# ──────────────────────────────────────────────
_NGRAM_DETECTOR: NgramDetector | None = None


def _get_ngram_detector() -> NgramDetector:
    """모듈 레벨 싱글턴으로 NgramDetector를 반환한다.
    references/ngram_labels.json이 없으면 빈 감지기를 반환."""
    global _NGRAM_DETECTOR
    if _NGRAM_DETECTOR is None:
        label_path = Path(__file__).parent / "references" / "ngram_labels.json"
        _NGRAM_DETECTOR = NgramDetector.from_json(label_path)
    return _NGRAM_DETECTOR

def srt_time_to_seconds(s: str) -> float:
    m = SRT_TIME_RE.fullmatch(s.strip())
    if not m:
        raise ValueError(f"잘못된 SRT 시각: {s}")
    h, mi, se, ms = map(int, m.groups())
    return h * 3600 + mi * 60 + se + ms / 1000.0


def seconds_to_srt_time(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def seconds_to_hms(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def seconds_to_ffmpeg(t: float) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def parse_time_spec(s: str) -> float:
    """MM:SS 또는 HH:MM:SS 또는 초 단위 숫자를 받아 초로 변환."""
    parts = s.split(":")
    try:
        if len(parts) == 1:
            return float(parts[0])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    raise SystemExit(f"시간 형식 오류 '{s}': MM:SS 또는 HH:MM:SS")


def parse_srt(path: Path) -> list[Segment]:
    text = path.read_text(encoding="utf-8")
    out: list[Segment] = []
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue
        try:
            idx = int(lines[0])
            timing = lines[1]
            content = "\n".join(lines[2:]).strip()
            start_s, end_s = timing.split(" --> ")
            out.append(
                Segment(
                    index=idx,
                    start=srt_time_to_seconds(start_s),
                    end=srt_time_to_seconds(end_s),
                    text=content,
                )
            )
        except (ValueError, IndexError):
            continue
    return out


def find_end_segment(
    segments: list[Segment],
    patterns: list[re.Pattern],
    max_end_seconds: float,
    window: int = 4,
) -> Segment | None:
    """슬라이딩 윈도우로 인접 segment를 묶어 끝 마커를 찾는다.

    마커 멘트가 두세 segment에 걸쳐 끊겨 있어도 매칭되며,
    마지막 매칭 segment(=실제 "여기까지/마무리" 단어가 들어 있는 segment)를 반환.
    """
    min_end_seconds = 600.0  # 최소 10분 이후에만 코너 종료를 감지하도록 안전 제한
    for i, seg in enumerate(segments):
        if seg.start < min_end_seconds:
            continue
        if seg.start > max_end_seconds:
            return None
        end_i = min(i + window, len(segments))
        combined = " ".join(segments[j].text for j in range(i, end_i))
        if not any(p.search(combined) for p in patterns):
            continue
        # combined에서 매칭됐으니, 윈도우 내에서 "여기까지/마무리"가 들어있는
        # 마지막 segment를 코너 종료점으로 잡는다.
        last_match: Segment | None = None
        for j in range(i, end_i):
            if CLOSING_TAIL_RE.search(segments[j].text):
                last_match = segments[j]
        return last_match or seg
    return None


def find_start_segment(
    segments: list[Segment],
    patterns: list[re.Pattern],
    max_start_seconds: float,
    window: int = 4,
    intro_patterns: list[re.Pattern] = None,
    welcome_patterns: list[re.Pattern] = None,
) -> tuple[Segment | None, str]:
    """다단계(오프닝 시그니처 -> 고도화된 클립 마커 -> 역방향 탐색) 탐색으로 시작 세그먼트를 찾는다.
    
    반환값: (Segment, 감지_방식_문자열)
    """
    # ----------------------------------------------------
    # 단계 1: 코너 시그니처 오프닝 검출 (Pass 1)
    # ----------------------------------------------------
    # 1.5분(90초) ~ max_start_seconds 사이를 대상으로 시그니처 멘트 검색
    min_search = 90.0
    if intro_patterns and welcome_patterns:
        for i, seg in enumerate(segments):
            if seg.start < min_search:
                continue
            if seg.start > max_start_seconds:
                break
            
            end_i = min(i + window, len(segments))
            combined = " ".join(segments[j].text for j in range(i, end_i))
            
            # 코너명 시그니처와 게스트 웰컴/인트로 관련 키워드가 한 윈도우 내에 함께 존재할 때
            if any(p.search(combined) for p in intro_patterns) and any(p.search(combined) for p in welcome_patterns):
                name_re = re.compile(r"스크린|잉글리|Screen|English", re.IGNORECASE)
                for j in range(i, end_i):
                    if name_re.search(segments[j].text):
                        return segments[j], "시그니처(인트로)"
                return seg, "시그니처(인트로)"

    # ----------------------------------------------------
    # 단계 2: 영화 클립 시작 마커 검출 (Pass 2) + 역방향 오프닝 매핑
    # ----------------------------------------------------
    start_seg = None
    start_tail = re.compile(r"장면|듣고|들어\s*[보봐]", re.IGNORECASE)
    
    for i, seg in enumerate(segments):
        if seg.start > max_start_seconds:
            break
        end_i = min(i + window, len(segments))
        combined = " ".join(segments[j].text for j in range(i, end_i))
        if any(p.search(combined) for p in patterns):
            # 윈도우 내에서 키워드가 처음 나오는 세그먼트 반환
            for j in range(i, end_i):
                if start_tail.search(segments[j].text):
                    start_seg = segments[j]
                    break
            if start_seg is None:
                start_seg = seg
            break

    if start_seg is not None:
        # 클립 시작부를 찾은 경우, 역방향(최대 6분/360초 전)으로 거슬러 올라가며
        # "스크린 잉글리쉬/Screen English" 등 오프닝이나 게스트 인사를 언급하는 세그먼트가 있는지 확인
        lookback_limit = start_seg.start - 360.0
        earliest_intro_seg = start_seg
        
        lookback_keywords = re.compile(
            r"스크린\s*잉글리[쉬시]|Screen\s*English|Chris|크리스|good\s*morning|굿모닝|welcome|웰컴|theater|시어터|타임|time",
            re.IGNORECASE
        )
        for seg in reversed(segments):
            if seg.start < lookback_limit:
                break
            if seg.start >= start_seg.start:
                continue
            if lookback_keywords.search(seg.text):
                earliest_intro_seg = seg
                
        if earliest_intro_seg != start_seg:
            return earliest_intro_seg, f"클립마커(역방향-seg#{start_seg.index})"
        return start_seg, "클립마커"

    return None, "없음"


def find_start_seconds_fallback(
    segments: list[Segment],
    end_seg: Segment,
    intro_skip: float,
    max_duration: float,
) -> float:
    """START 마커 검출 실패 시 휴리스틱: 인트로 스킵 후 첫 segment."""
    earliest = max(intro_skip, end_seg.start - max_duration)
    for seg in segments:
        if seg.start >= earliest:
            return seg.start
    return earliest


def file_date(path: Path) -> date | None:
    m = DATE_PREFIX_RE.match(path.name)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as e:
        raise SystemExit(f"날짜 형식 오류 '{s}': YYYY-MM-DD") from e


def collect_audio_files(
    input_path: Path,
    on_date: date | None,
    from_date: date | None,
    to_date: date | None,
    name_filter: str | None,
) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    files = sorted(
        p for p in input_path.iterdir() if p.suffix.lower() in AUDIO_EXTS
    )
    out: list[Path] = []
    for p in files:
        if name_filter and name_filter.lower() not in p.name.lower():
            continue
        d = file_date(p)
        if on_date and d != on_date:
            continue
        if from_date and (d is None or d < from_date):
            continue
        if to_date and (d is None or d > to_date):
            continue
        out.append(p)
    return out


def find_ffmpeg() -> str:
    """PATH 우선 검색하고, 없으면 흔한 설치 경로를 직접 뒤져본다."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    candidates = [
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        os.path.expandvars(
            r"%LOCALAPPDATA%\Microsoft\WinGet\Links\ffmpeg.exe"
        ),
    ]
    # winget 설치 위치는 패키지 ID 기반 폴더라 glob으로 찾는다
    winget_root = Path(os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
    ))
    if winget_root.is_dir():
        for p in winget_root.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"):
            candidates.append(str(p))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    raise SystemExit(
        "ffmpeg을 찾을 수 없습니다. 설치 또는 PATH 추가가 필요합니다.\n"
        "  winget install Gyan.FFmpeg  또는  choco install ffmpeg"
    )


_FFMPEG_PATH: str | None = None


def extract_audio(src: Path, dest: Path, start: float, end: float) -> None:
    global _FFMPEG_PATH
    if _FFMPEG_PATH is None:
        _FFMPEG_PATH = find_ffmpeg()
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _FFMPEG_PATH, "-y",
        "-i", str(src),
        "-ss", seconds_to_ffmpeg(start),
        "-to", seconds_to_ffmpeg(end),
        "-c", "copy",
        str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr.decode("utf-8", errors="replace"))
        raise SystemExit(f"ffmpeg 실패 (code {result.returncode}): {src.name}")


def write_outputs(
    dest_stem: Path,
    segments: list[Segment],
    start: float,
    end: float,
    audio_name: str,
    corner_label: str,
) -> tuple[Path, Path, Path]:
    filtered: list[Segment] = []
    for s in segments:
        if s.end <= start or s.start >= end:
            continue
        new_start = max(0.0, s.start - start)
        new_end = max(0.0, min(s.end, end) - start)
        filtered.append(
            Segment(len(filtered) + 1, new_start, new_end, s.text)
        )

    txt_path = dest_stem.with_suffix(".txt")
    srt_path = dest_stem.with_suffix(".srt")
    md_path = dest_stem.with_suffix(".md")

    with open(txt_path, "w", encoding="utf-8") as f:
        for seg in filtered:
            f.write(seg.text + "\n")

    with open(srt_path, "w", encoding="utf-8") as f:
        for seg in filtered:
            f.write(
                f"{seg.index}\n"
                f"{seconds_to_srt_time(seg.start)} --> {seconds_to_srt_time(seg.end)}\n"
                f"{seg.text}\n\n"
            )

    duration = end - start
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# {audio_name} — {corner_label}\n\n")
        f.write(
            f"- 원본 시간 범위: {seconds_to_hms(start)} ~ {seconds_to_hms(end)}\n"
            f"- 코너 길이: {seconds_to_hms(duration)}\n"
            f"- segment 수: {len(filtered)}\n\n"
        )
        f.write("---\n\n")
        if not filtered:
            return txt_path, srt_path, md_path

        paragraph_gap = 1.5
        marker_interval = 60.0
        paragraph: list[str] = []
        para_start = filtered[0].start
        last_marker = -marker_interval
        prev_end = filtered[0].start

        def flush() -> None:
            if not paragraph:
                return
            f.write(f"**[{seconds_to_hms(para_start)}]** ")
            f.write(" ".join(paragraph).strip())
            f.write("\n\n")

        for seg in filtered:
            gap = seg.start - prev_end
            crossed = seg.start - last_marker >= marker_interval
            if paragraph and (gap >= paragraph_gap or crossed):
                flush()
                paragraph = []
                para_start = seg.start
                last_marker = seg.start
            paragraph.append(seg.text)
            prev_end = seg.end
        flush()

    return txt_path, srt_path, md_path


def process_one(
    audio_path: Path,
    corner: dict,
    corner_id: str,
    out_dir: Path,
    manual_start: float | None,
    manual_end: float | None,
    overwrite: bool,
    dry_run: bool,
) -> bool:
    srt_path = audio_path.with_suffix(".srt")
    if not srt_path.exists():
        print(f"  스킵 (.srt 없음, 전사 진행 중일 수 있음): {audio_path.name}")
        return False

    segments = parse_srt(srt_path)
    if not segments:
        print(f"  스킵 (segment 0개): {audio_path.name}")
        return False

    if manual_end is not None:
        end_s = manual_end
        end_info = "수동"
    else:
        # 0순위: N-gram 유사도 기반 종료 감지
        nd = _get_ngram_detector()
        ng_end = nd.find_end(segments)
        if ng_end is not None:
            end_seg_ng, end_score = ng_end
            end_s = end_seg_ng.end
            end_info = f"ngram(seg#{end_seg_ng.index} @ {seconds_to_hms(end_seg_ng.start)}, score={end_score:.3f})"
        else:
            # 1순위: 기존 regex 종료 감지
            end_seg = find_end_segment(
                segments,
                corner["end_patterns"],
                corner["max_end_minutes"] * 60,
                window=corner.get("end_window_segments", 4),
            )
            if end_seg is None:
                print(f"  스킵 (끝 마커 못 찾음): {audio_path.name}")
                return False
            end_s = end_seg.end
            end_info = f"regex(seg#{end_seg.index} @ {seconds_to_hms(end_seg.start)})"

    if manual_start is not None:
        start_s = manual_start
        start_info = "수동"
    else:
        # 0순위: N-gram 유사도 기반 시작 감지
        nd = _get_ngram_detector()
        ng_start = nd.find_start(segments)
        if ng_start is not None:
            start_seg_ng, start_score = ng_start
            # N-gram으로 클립 시작점을 찾은 후, 역방향 오프닝 탐색도 수행
            lookback_limit = start_seg_ng.start - 360.0
            earliest_intro_seg = start_seg_ng
            lookback_keywords = re.compile(
                r"스크린\s*잉글리[쉬시]|Screen\s*English|Chris|크리스|good\s*morning|굿모닝|welcome|웰컴|theater|시어터|타임|time",
                re.IGNORECASE
            )
            for seg in reversed(segments):
                if seg.start < lookback_limit:
                    break
                if seg.start >= start_seg_ng.start:
                    continue
                if lookback_keywords.search(seg.text):
                    earliest_intro_seg = seg
            if earliest_intro_seg != start_seg_ng:
                start_s = earliest_intro_seg.start
                start_info = f"ngram+역방향(seg#{start_seg_ng.index}, score={start_score:.3f})"
            else:
                start_s = start_seg_ng.start
                start_info = f"ngram(seg#{start_seg_ng.index} @ {seconds_to_hms(start_seg_ng.start)}, score={start_score:.3f})"
        else:
            # 1순위: 기존 regex START 마커 검출
            start_seg, start_method = find_start_segment(
                segments,
                corner.get("start_patterns", []),
                corner.get("start_max_minutes", 12) * 60,
                window=corner.get("start_window_segments", 4),
                intro_patterns=corner.get("intro_patterns"),
                welcome_patterns=corner.get("welcome_patterns"),
            ) if corner.get("start_patterns") else (None, "없음")
            if start_seg is not None:
                start_s = start_seg.start
                start_info = f"regex-{start_method}(seg#{start_seg.index} @ {seconds_to_hms(start_seg.start)})"
            else:
                # 폴백 (커버리지 우선): 인트로 스킵 후 첫 segment
                start_s = find_start_seconds_fallback(
                    segments,
                    Segment(0, end_s, end_s, ""),
                    corner["intro_skip_seconds"],
                    corner["max_duration_minutes"] * 60,
                )
                start_info = "폴백(인트로 스킵)"

    if end_s <= start_s:
        print(f"  스킵 (start >= end): {audio_path.name}")
        return False

    d = file_date(audio_path)
    prefix = d.strftime("%Y-%m-%d") if d else "nodate"
    dest_stem = out_dir / f"{prefix}_{corner_id}"
    dest_mp3 = dest_stem.with_suffix(audio_path.suffix.lower())

    print(f"  {audio_path.name}")
    print(
        f"    범위: {seconds_to_hms(start_s)} ~ {seconds_to_hms(end_s)} "
        f"({seconds_to_hms(end_s - start_s)}) | start: {start_info} | end: {end_info}"
    )
    print(f"    -> {dest_mp3.relative_to(out_dir.parent) if out_dir.parent in dest_mp3.parents else dest_mp3}")

    if dry_run:
        return True

    if dest_mp3.exists() and not overwrite:
        print("    스킵 (이미 존재, --overwrite로 강제 가능)")
    else:
        extract_audio(audio_path, dest_mp3, start_s, end_s)

    write_outputs(
        dest_stem=dest_stem,
        segments=segments,
        start=start_s,
        end=end_s,
        audio_name=audio_path.name,
        corner_label=corner["label"],
    )
    return True


def main() -> int:
    p = argparse.ArgumentParser(description="방송 mp3에서 특정 코너 추출")
    p.add_argument("input", help="오디오 파일 또는 폴더")
    p.add_argument(
        "--corner",
        choices=sorted(CORNER_DEFS.keys()),
        default="screen_english",
        help="추출할 코너 id (기본: screen_english)",
    )
    p.add_argument(
        "--name-filter",
        default=None,
        help="파일명에 포함되어야 하는 문자열 (예: 'Screen English'). "
             "폴더 입력 시 코너에 해당하는 회차만 고를 때 사용.",
    )
    p.add_argument(
        "--out-dir",
        default=None,
        help="출력 폴더 (기본: <input>/corners 또는 input 파일의 부모/corners)",
    )

    sel = p.add_argument_group("날짜 필터 (폴더 입력 시)")
    sel.add_argument("--date", dest="on_date", default=None, help="특정 날짜 YYYY-MM-DD")
    sel.add_argument("--from", dest="from_date", default=None, help="시작 날짜 (포함)")
    sel.add_argument("--to", dest="to_date", default=None, help="종료 날짜 (포함)")

    man = p.add_argument_group("수동 시간 지정 (단일 파일 권장)")
    man.add_argument("--start", default=None, help="시작 시각 MM:SS 또는 HH:MM:SS")
    man.add_argument("--end", default=None, help="종료 시각 MM:SS 또는 HH:MM:SS")

    p.add_argument("--overwrite", action="store_true", help="기존 결과 덮어쓰기")
    p.add_argument("--dry-run", action="store_true", help="검출 결과만 출력")

    args = p.parse_args()

    on_date = parse_date(args.on_date) if args.on_date else None
    from_date = parse_date(args.from_date) if args.from_date else None
    to_date = parse_date(args.to_date) if args.to_date else None

    manual_start = parse_time_spec(args.start) if args.start else None
    manual_end = parse_time_spec(args.end) if args.end else None

    input_path = Path(args.input)
    if not input_path.exists():
        raise SystemExit(f"경로 없음: {input_path}")

    corner = CORNER_DEFS[args.corner]
    # 사용자가 별도 name-filter를 안 주고 화면영어를 추출한다면 코너명 자동 매칭
    name_filter = args.name_filter
    if name_filter is None and args.corner == "screen_english":
        name_filter = "Screen English"

    base_dir = input_path.parent if input_path.is_file() else input_path
    out_dir = Path(args.out_dir) if args.out_dir else base_dir / "corners"

    files = collect_audio_files(input_path, on_date, from_date, to_date, name_filter)
    if not files:
        print("처리할 파일이 없습니다.")
        return 1

    print(f"코너: {corner['label']} ({args.corner})")
    print(f"대상 {len(files)}개, 출력: {out_dir}\n")

    ok = 0
    for f in files:
        if process_one(
            audio_path=f,
            corner=corner,
            corner_id=args.corner,
            out_dir=out_dir,
            manual_start=manual_start,
            manual_end=manual_end,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        ):
            ok += 1

    print(f"\n완료: {ok}/{len(files)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
