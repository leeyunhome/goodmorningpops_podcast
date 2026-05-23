"""faster-whisper로 오디오 파일(들)을 한국어 스크립트로 전사한다.

단일 파일 또는 폴더를 받을 수 있으며, 파일명 앞에 붙은 YYYY-MM-DD 접두사
(fetch_episode.py가 생성하는 형식)를 기준으로 날짜 필터링도 지원한다.

기본 모델은 large-v3-turbo이며, GPU(CUDA)가 있으면 자동으로 활용한다.
출력 포맷(기본 모두 생성):
  .txt  segment 단위 평문
  .srt  자막
  .md   읽기 좋은 스크립트(단락 묶기 + 타임스탬프 마커)
  .json 메타데이터 + 모든 segment (--formats 로 활성화)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

# Windows에서 ctranslate2가 PyTorch 번들 cuDNN/CUDA DLL을 찾을 수 있도록
# faster_whisper보다 먼저 torch를 import하고 lib 폴더를 PATH에 등록한다.
# os.add_dll_directory만으로는 ctranslate2의 C++ LoadLibrary가 참조하지 않으므로
# os.environ["PATH"]에도 직접 추가한다.
if sys.platform == "win32":
    try:
        import torch  # noqa: F401
        _torch_lib = os.path.join(os.path.dirname(torch.__file__), "lib")
        if os.path.isdir(_torch_lib):
            os.add_dll_directory(_torch_lib)
            os.environ["PATH"] = _torch_lib + os.pathsep + os.environ.get("PATH", "")
    except ImportError:
        pass

from faster_whisper import WhisperModel
from tqdm import tqdm

AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac", ".webm"}
DATE_PREFIX_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
DEFAULT_FORMATS = "txt,srt,md"
ALL_FORMATS = {"txt", "srt", "md", "json"}


@dataclass
class Segment:
    start: float
    end: float
    text: str


def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def detect_device(force: str = "auto") -> tuple[str, str]:
    if force == "cpu":
        return "cpu", "int8"
    if force == "cuda":
        return "cuda", "float16"
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda", "float16"
    except ImportError:
        pass
    return "cpu", "int8"


def parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as e:
        raise SystemExit(f"날짜 형식 오류 '{s}': YYYY-MM-DD 형식이어야 합니다.") from e


def file_date(path: Path) -> date | None:
    m = DATE_PREFIX_RE.match(path.name)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def collect_audio(
    input_path: Path,
    on_date: date | None,
    from_date: date | None,
    to_date: date | None,
) -> list[Path]:
    if input_path.is_file():
        return [input_path]
    if not input_path.is_dir():
        raise SystemExit(f"경로가 없습니다: {input_path}")

    files = sorted(
        p for p in input_path.iterdir() if p.suffix.lower() in AUDIO_EXTS
    )

    date_filter_used = on_date or from_date or to_date
    if not date_filter_used:
        return files

    out = []
    for p in files:
        d = file_date(p)
        if d is None:
            continue
        if on_date and d != on_date:
            continue
        if from_date and d < from_date:
            continue
        if to_date and d > to_date:
            continue
        out.append(p)
    return out


def hms(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def write_txt(path: Path, segments: list[Segment]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for seg in segments:
            f.write(seg.text + "\n")


def write_srt(path: Path, segments: list[Segment]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, start=1):
            f.write(
                f"{i}\n"
                f"{format_timestamp(seg.start)} --> {format_timestamp(seg.end)}\n"
                f"{seg.text}\n\n"
            )


def write_md(
    path: Path,
    segments: list[Segment],
    audio_name: str,
    duration: float,
    language: str,
    paragraph_gap: float = 1.5,
    marker_interval: float = 60.0,
) -> None:
    """단락 묶기 + 주기적 타임스탬프 마커가 있는 읽기용 스크립트."""
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# {audio_name}\n\n")
        f.write(
            f"- 길이: {hms(duration)}\n"
            f"- 언어: {language}\n"
            f"- 전사: faster-whisper\n\n"
        )
        f.write("---\n\n")

        if not segments:
            return

        paragraph: list[str] = []
        paragraph_start: float = segments[0].start
        last_marker: float = -marker_interval
        prev_end: float = segments[0].start

        def flush() -> None:
            if not paragraph:
                return
            f.write(f"**[{hms(paragraph_start)}]** ")
            f.write(" ".join(paragraph).strip())
            f.write("\n\n")

        for seg in segments:
            gap = seg.start - prev_end
            crossed_marker = seg.start - last_marker >= marker_interval

            if paragraph and (gap >= paragraph_gap or crossed_marker):
                flush()
                paragraph = []
                paragraph_start = seg.start
                last_marker = seg.start

            paragraph.append(seg.text)
            prev_end = seg.end

        flush()


def write_json(
    path: Path,
    segments: list[Segment],
    audio_name: str,
    duration: float,
    language: str,
    language_probability: float,
) -> None:
    payload = {
        "audio": audio_name,
        "duration": duration,
        "language": language,
        "language_probability": language_probability,
        "segments": [
            {"start": seg.start, "end": seg.end, "text": seg.text}
            for seg in segments
        ],
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def output_paths(audio_path: Path, formats: set[str]) -> dict[str, Path]:
    return {fmt: audio_path.with_suffix(f".{fmt}") for fmt in formats}


def transcribe_files(
    files: list[Path],
    model_name: str = "large-v3-turbo",
    language: str = "ko",
    beam_size: int = 5,
    vad: bool = True,
    initial_prompt: str | None = None,
    overwrite: bool = False,
    formats: set[str] | None = None,
    device: str = "auto",
) -> None:
    """모델을 한 번만 로딩하고 여러 파일을 일괄 전사한다."""
    if not files:
        return
    if formats is None:
        formats = {"txt", "srt", "md"}

    device, compute_type = detect_device(device)
    print(f"\n디바이스: {device} ({compute_type}), 모델: {model_name}")
    print("모델 로딩 중... (최초 1회는 다운로드 시간이 걸립니다)")
    model = WhisperModel(model_name, device=device, compute_type=compute_type)

    for audio_path in files:
        transcribe_one(
            model=model,
            audio_path=audio_path,
            language=language,
            beam_size=beam_size,
            vad=vad,
            initial_prompt=initial_prompt,
            overwrite=overwrite,
            formats=formats,
        )


def transcribe_one(
    model: WhisperModel,
    audio_path: Path,
    language: str,
    beam_size: int,
    vad: bool,
    initial_prompt: str | None,
    overwrite: bool,
    formats: set[str],
) -> None:
    outs = output_paths(audio_path, formats)

    if not overwrite and all(p.exists() for p in outs.values()):
        print(f"건너뜀(이미 존재): {audio_path.name}")
        return

    print(f"\n전사 시작: {audio_path.name}")
    # PyAV(libavformat)가 Windows에서 비-ASCII 경로(한글 등)를 열지 못하는
    # 문제를 우회하기 위해 바이너리 파일 핸들로 전달한다.
    with open(audio_path, "rb") as audio_fp:
        segments_iter, info = model.transcribe(
            audio_fp,
            language=language,
            beam_size=beam_size,
            vad_filter=vad,
            vad_parameters={"min_silence_duration_ms": 500} if vad else None,
            initial_prompt=initial_prompt,
            word_timestamps=False,
        )
        print(
            f"  감지 언어: {info.language} (확률 {info.language_probability:.2f}), "
            f"길이: {info.duration:.1f}s"
        )

        segments: list[Segment] = []
        with tqdm(total=info.duration, unit="s", desc="  진행") as bar:
            last_end = 0.0
            for seg in segments_iter:
                segments.append(Segment(seg.start, seg.end, seg.text.strip()))
                bar.update(max(0.0, seg.end - last_end))
                last_end = seg.end

    written: list[str] = []
    if "txt" in formats:
        write_txt(outs["txt"], segments)
        written.append(outs["txt"].name)
    if "srt" in formats:
        write_srt(outs["srt"], segments)
        written.append(outs["srt"].name)
    if "md" in formats:
        write_md(
            outs["md"],
            segments,
            audio_name=audio_path.name,
            duration=info.duration,
            language=info.language,
        )
        written.append(outs["md"].name)
    if "json" in formats:
        write_json(
            outs["json"],
            segments,
            audio_name=audio_path.name,
            duration=info.duration,
            language=info.language,
            language_probability=info.language_probability,
        )
        written.append(outs["json"].name)

    print(f"  완료: {', '.join(written)}")


def main() -> int:
    p = argparse.ArgumentParser(description="faster-whisper 한국어 전사기")
    p.add_argument(
        "input",
        help="오디오 파일 또는 폴더 경로 (폴더면 내부의 오디오 파일 일괄 처리)",
    )
    p.add_argument(
        "--model",
        default="large-v3-turbo",
        help="모델 이름 (large-v3-turbo, large-v3, medium, small 등)",
    )
    p.add_argument("--language", default="ko", help="언어 코드 (기본: ko)")
    p.add_argument("--beam-size", type=int, default=5, help="빔 서치 크기 (기본: 5)")
    p.add_argument(
        "--no-vad",
        action="store_true",
        help="VAD(무음 제거) 비활성화. 기본은 활성화",
    )
    p.add_argument(
        "--prompt",
        default=None,
        help="초기 프롬프트. 고유명사/외래어 표기를 유도할 때 사용",
    )
    p.add_argument(
        "--formats",
        default=DEFAULT_FORMATS,
        help=(
            f"쉼표로 구분한 출력 포맷 (기본: {DEFAULT_FORMATS}). "
            f"선택: {','.join(sorted(ALL_FORMATS))}"
        ),
    )
    p.add_argument(
        "--device",
        choices=["auto", "cpu", "cuda"],
        default="auto",
        help="추론 디바이스 (기본: auto - GPU 있으면 cuda)",
    )

    date_group = p.add_argument_group(
        "날짜 필터 (폴더 입력 시, 파일명 앞 YYYY-MM-DD 기준)"
    )
    date_group.add_argument("--date", dest="on_date", default=None, help="특정 날짜")
    date_group.add_argument("--from", dest="from_date", default=None, help="시작 날짜 (포함)")
    date_group.add_argument("--to", dest="to_date", default=None, help="종료 날짜 (포함)")

    p.add_argument(
        "--overwrite",
        action="store_true",
        help="이미 .txt/.srt가 있어도 다시 전사",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="전사 없이 처리 대상 파일만 출력",
    )
    args = p.parse_args()

    on_date = parse_date(args.on_date) if args.on_date else None
    from_date = parse_date(args.from_date) if args.from_date else None
    to_date = parse_date(args.to_date) if args.to_date else None

    if on_date and (from_date or to_date):
        raise SystemExit("--date 와 --from/--to 는 함께 사용할 수 없습니다.")
    if from_date and to_date and from_date > to_date:
        raise SystemExit("--from 이 --to 보다 늦습니다.")

    formats = {f.strip().lower() for f in args.formats.split(",") if f.strip()}
    unknown = formats - ALL_FORMATS
    if unknown:
        raise SystemExit(
            f"알 수 없는 포맷: {','.join(sorted(unknown))}. "
            f"선택: {','.join(sorted(ALL_FORMATS))}"
        )
    if not formats:
        raise SystemExit("--formats 가 비어 있습니다.")

    input_path = Path(args.input)
    files = collect_audio(input_path, on_date, from_date, to_date)

    if not files:
        print("처리할 오디오 파일이 없습니다.", file=sys.stderr)
        return 1

    print(f"처리 대상 {len(files)}개:")
    for f in files:
        print(f"  {f}")
    if args.dry_run:
        return 0

    transcribe_files(
        files=files,
        model_name=args.model,
        language=args.language,
        beam_size=args.beam_size,
        vad=not args.no_vad,
        initial_prompt=args.prompt,
        overwrite=args.overwrite,
        formats=formats,
        device=args.device,
    )

    print("\n모든 전사 완료.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
