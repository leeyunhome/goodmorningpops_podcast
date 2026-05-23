"""mp3 파일을 음성 콘텐츠용으로 재인코딩해 용량을 줄인다.

기본: 모노 + 64 kbps — 음성에 충분, 원본 대비 70~80% 절감.
더 줄이려면 --bitrate 48k 또는 32k (32k는 약간 음질 저하 체감 가능).

사용 예:
  python optimize_mp3.py audio/corners
  python optimize_mp3.py audio/corners --bitrate 48k --overwrite
  python optimize_mp3.py audio/corners --out-dir web_audio
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


def find_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    candidates = [
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    ]
    winget_root = Path(
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    )
    if winget_root.is_dir():
        for p in winget_root.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"):
            candidates.append(str(p))
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    raise SystemExit(
        "ffmpeg을 찾을 수 없습니다.\n  winget install Gyan.FFmpeg"
    )


_FFMPEG: str | None = None


def reencode(src: Path, dst: Path, bitrate: str, mono: bool) -> None:
    global _FFMPEG
    if _FFMPEG is None:
        _FFMPEG = find_ffmpeg()
    dst.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        _FFMPEG, "-y", "-i", str(src),
        "-c:a", "libmp3lame",
        "-b:a", bitrate,
    ]
    if mono:
        cmd += ["-ac", "1"]
    cmd.append(str(dst))
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        sys.stderr.write(result.stderr.decode("utf-8", errors="replace"))
        raise SystemExit(f"ffmpeg 실패: {src.name}")


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    return f"{n/1024/1024:.1f} MB"


def main() -> int:
    p = argparse.ArgumentParser(
        description="mp3 재인코딩으로 용량 절약 (음성 콘텐츠용)"
    )
    p.add_argument("input", help="입력 폴더 또는 단일 mp3 파일")
    p.add_argument(
        "--out-dir",
        default=None,
        help="출력 폴더 (기본: <input>/optimized 또는 단일 파일 부모의 optimized/)",
    )
    p.add_argument(
        "--bitrate",
        default="64k",
        help="비트레이트. 32k/48k/64k/96k (기본: 64k). 음성은 64k 권장.",
    )
    p.add_argument(
        "--stereo",
        action="store_true",
        help="스테레오 유지 (기본: 모노 변환)",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="이미 결과 파일이 있어도 다시 인코딩",
    )
    args = p.parse_args()

    src_path = Path(args.input)
    if src_path.is_file():
        files = [src_path]
        out_dir = (
            Path(args.out_dir) if args.out_dir else src_path.parent / "optimized"
        )
    elif src_path.is_dir():
        files = sorted(src_path.glob("*.mp3"))
        out_dir = Path(args.out_dir) if args.out_dir else src_path / "optimized"
    else:
        raise SystemExit(f"경로 없음: {src_path}")

    if not files:
        raise SystemExit("처리할 mp3 없음")

    print(f"대상 {len(files)}개")
    print(f"출력: {out_dir}")
    print(f"설정: {args.bitrate}  {'스테레오' if args.stereo else '모노'}")
    print()

    total_in = 0
    total_out = 0
    skipped = 0
    for i, src in enumerate(files, 1):
        dst = out_dir / src.name
        if dst.exists() and not args.overwrite:
            print(f"  [{i}/{len(files)}] 스킵 (이미 존재): {src.name}")
            skipped += 1
            continue
        size_in = src.stat().st_size
        reencode(src, dst, args.bitrate, mono=not args.stereo)
        size_out = dst.stat().st_size
        total_in += size_in
        total_out += size_out
        reduction = (1 - size_out / size_in) * 100 if size_in else 0
        print(
            f"  [{i}/{len(files)}] {src.name}\n"
            f"      {human_size(size_in)} -> {human_size(size_out)}  "
            f"({reduction:.0f}% 절감)"
        )

    print()
    print(f"완료: {len(files) - skipped}개 처리, {skipped}개 스킵")
    if total_in > 0:
        overall = (1 - total_out / total_in) * 100
        print(f"총합: {human_size(total_in)} -> {human_size(total_out)}  ({overall:.0f}% 절감)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
