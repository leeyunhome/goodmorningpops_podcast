"""텍스트(.txt) 또는 PDF(.pdf) 파일을 MP3로 변환한다 (Edge TTS).

- 무료, API 키 없음, 한국어 신경망 음성
- PDF는 pypdf로 텍스트 추출, 텍스트가 거의 없으면 OCR fallback (옵션)

사용 예:
  python tts.py references/notes.txt
  python tts.py references/OPIc_AL_Final_Audio_Guide_Handbook.pdf --rate +10%
  python tts.py document.pdf --voice ko-KR-InJoonNeural
  python tts.py document.md --elsa
  python tts.py document.md --sherlock
  python tts.py --list-voices

OCR 사용 시 시스템 의존성 필요:
  winget install UB-Mannheim.TesseractOCR
  winget install oschwartz10612.Poppler
  pip install pytesseract pdf2image
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

# Windows cp949 콘솔 한글 출력 보호
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

DEFAULT_KO_VOICE = "ko-KR-SunHiNeural"
DEFAULT_EN_VOICE = "en-US-AvaNeural"
# Edge TTS는 한 번에 ~10K자도 가능하지만 안정성/실패시 재시도 비용 고려해 보수적으로
MAX_CHUNK_CHARS = 3000

# 영어 액센트 단축키. (f=female 기본, m=male)
# 일본/한국식은 해당 언어 음성에 영어를 먹이는 방식 (외국인 영어 느낌의 시뮬레이션).
ACCENT_PRESETS = {
    # 미국 (기본)
    "us":   "en-US-AvaNeural",
    "us-f": "en-US-AvaNeural",
    "us-m": "en-US-AndrewNeural",
    # 영국
    "gb":   "en-GB-SoniaNeural",
    "gb-f": "en-GB-SoniaNeural",
    "gb-m": "en-GB-RyanNeural",
    "uk":   "en-GB-SoniaNeural",  # alias
    "uk-f": "en-GB-SoniaNeural",
    "uk-m": "en-GB-RyanNeural",
    # 호주
    "au":   "en-AU-NatashaNeural",
    "au-f": "en-AU-NatashaNeural",
    "au-m": "en-AU-WilliamNeural",
    # 인도
    "in":   "en-IN-NeerjaNeural",
    "in-f": "en-IN-NeerjaNeural",
    "in-m": "en-IN-PrabhatNeural",
    # 아일랜드
    "ie":   "en-IE-EmilyNeural",
    "ie-f": "en-IE-EmilyNeural",
    "ie-m": "en-IE-ConnorNeural",
    # 캐나다
    "ca":   "en-CA-ClaraNeural",
    "ca-f": "en-CA-ClaraNeural",
    "ca-m": "en-CA-LiamNeural",
    # 뉴질랜드
    "nz":   "en-NZ-MollyNeural",
    "nz-m": "en-NZ-MitchellNeural",
    # 싱가포르
    "sg":   "en-SG-LunaNeural",
    "sg-m": "en-SG-WayneNeural",
    # 일본식 영어 (일본어 음성이 영어 발화 — 일본식 발음 시뮬레이션)
    "jp":   "ja-JP-NanamiNeural",
    "jp-f": "ja-JP-NanamiNeural",
    "jp-m": "ja-JP-KeitaNeural",
    # 한국식 영어 (한국어 음성이 영어 발화)
    "ko":   "ko-KR-SunHiNeural",
    "ko-f": "ko-KR-SunHiNeural",
    "ko-m": "ko-KR-InJoonNeural",
}

# 한글/영문 감지용
HANGUL_RE = re.compile(r"[가-힣]")
LATIN_RE = re.compile(r"[A-Za-z]")
# 문장 경계: 종결 부호 후 공백 또는 줄바꿈
SENT_BOUNDARY_RE = re.compile(r"(?<=[.!?。?!])\s+|\n+")


def detect_lang(text: str) -> str | None:
    """문장의 주력 언어 판별. 한글이 라틴 문자보다 많으면 'ko', 적으면 'en'.

    한글/라틴 둘 다 없으면 None (숫자/기호만).
    """
    h = len(HANGUL_RE.findall(text))
    l = len(LATIN_RE.findall(text))
    if h == 0 and l == 0:
        return None
    return "ko" if h >= l else "en"


def split_by_language(
    text: str, ko_voice: str, en_voice: str
) -> list[tuple[str, str]]:
    """문장 단위로 잘라 언어를 판별하고, 연속 같은 언어는 묶어 (voice, text)로 반환."""
    sentences = [s.strip() for s in SENT_BOUNDARY_RE.split(text) if s.strip()]
    runs: list[tuple[str, str]] = []
    cur_voice: str | None = None
    cur_text: list[str] = []

    def flush() -> None:
        if cur_text and cur_voice:
            runs.append((cur_voice, " ".join(cur_text)))

    for sent in sentences:
        lang = detect_lang(sent)
        voice = en_voice if lang == "en" else ko_voice
        if voice == cur_voice:
            cur_text.append(sent)
        else:
            flush()
            cur_voice = voice
            cur_text = [sent]
    flush()
    return runs


def extract_text_from_pdf(path: Path, ocr_mode: str = "auto") -> str:
    """PDF에서 텍스트 추출.

    ocr_mode:
      'never'   pypdf만 사용
      'auto'    pypdf 결과가 너무 적으면 OCR fallback (기본)
      'always'  무조건 OCR
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        raise SystemExit("pypdf가 필요합니다: pip install pypdf")

    if ocr_mode == "always":
        return run_ocr(path)

    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception as e:
            print(f"  (페이지 추출 경고: {e})", file=sys.stderr)
            pages.append("")
    text = "\n\n".join(pages).strip()

    # 페이지당 평균 50자 미만이면 스캔본일 가능성 큼
    if ocr_mode == "auto" and len(reader.pages) > 0:
        avg_per_page = len(text) / len(reader.pages)
        if avg_per_page < 50:
            print(f"  pypdf 추출이 빈약합니다 (평균 {avg_per_page:.0f}자/페이지). OCR 시도.")
            try:
                ocr_text = run_ocr(path)
                if len(ocr_text) > len(text):
                    return ocr_text
            except SystemExit as e:
                print(f"  OCR 사용 불가: {e}", file=sys.stderr)
                print("  pypdf 결과를 그대로 사용합니다.")
    return text


def run_ocr(path: Path) -> str:
    try:
        import pytesseract
        from pdf2image import convert_from_path
    except ImportError:
        raise SystemExit(
            "OCR 패키지가 없습니다.\n"
            "  pip install pytesseract pdf2image\n"
            "그리고 시스템에 Tesseract OCR + Poppler 설치 필요:\n"
            "  winget install UB-Mannheim.TesseractOCR\n"
            "  winget install oschwartz10612.Poppler"
        )
    print(f"  OCR 페이지 렌더링 중...")
    images = convert_from_path(str(path))
    parts = []
    for i, img in enumerate(images, 1):
        print(f"  OCR page {i}/{len(images)}")
        parts.append(pytesseract.image_to_string(img, lang="kor+eng"))
    return "\n\n".join(parts)


def read_input(path: Path, ocr_mode: str) -> str:
    ext = path.suffix.lower()
    if ext == ".txt":
        return path.read_text(encoding="utf-8")
    if ext == ".md":
        return path.read_text(encoding="utf-8")
    if ext == ".pdf":
        return extract_text_from_pdf(path, ocr_mode=ocr_mode)
    raise SystemExit(f"지원하지 않는 확장자: {ext} (.txt, .md, .pdf만 지원)")


def chunk_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """단락 → 문장 우선순위로 자연스럽게 분할."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    paragraphs = re.split(r"\n\s*\n", text)
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) > max_chars:
            flush()
            sentences = re.split(r"(?<=[.!?。?!])\s+|(?<=[.。])\s*", para)
            for sent in sentences:
                sent = sent.strip()
                if not sent:
                    continue
                if len(current) + len(sent) + 1 > max_chars:
                    flush()
                current = f"{current} {sent}".strip() if current else sent
            flush()
        else:
            if len(current) + len(para) + 2 > max_chars:
                flush()
            current = f"{current}\n\n{para}" if current else para
    flush()
    return chunks


async def synthesize(
    text: str,
    ko_voice: str,
    en_voice: str | None,
    rate: str,
    pitch: str,
    output: Path,
) -> None:
    """언어 감지 후 한국어/영어 음성을 자동 전환해 합성.

    en_voice=None 이면 모든 텍스트를 ko_voice 하나로 처리 (legacy --voice 호환).
    """
    try:
        import edge_tts
    except ImportError:
        raise SystemExit("edge-tts가 필요합니다: pip install edge-tts")

    # 언어별로 음성 매핑된 run 만들기
    if en_voice is None:
        runs = [(ko_voice, text)]
    else:
        runs = split_by_language(text, ko_voice, en_voice)

    # 각 run을 길이 제한에 맞춰 잘라 (voice, chunk) 리스트 만들기
    plan: list[tuple[str, str]] = []
    for voice, run_text in runs:
        for chunk in chunk_text(run_text):
            plan.append((voice, chunk))

    total_chars = sum(len(c) for _, c in plan)
    voices_used = sorted({v for v, _ in plan})
    print(
        f"합성 청크 {len(plan)}개, 총 {total_chars:,}자, "
        f"사용 음성: {', '.join(voices_used)}"
    )

    if output.exists():
        output.unlink()
    output.parent.mkdir(parents=True, exist_ok=True)

    for i, (voice, chunk) in enumerate(plan, 1):
        short = voice.split("-")[-1].replace("Neural", "")
        print(f"  [{i}/{len(plan)}] [{short:12s}] {len(chunk):,}자")
        try:
            communicate = edge_tts.Communicate(chunk, voice=voice, rate=rate, pitch=pitch)
            with open(output, "ab") as f:
                async for piece in communicate.stream():
                    if piece["type"] == "audio":
                        f.write(piece["data"])
        except Exception as e:
            print(f"  [경고] 합성 건너뜀 (발음할 수 없는 기호 등): {e}", file=sys.stderr)


async def list_available_voices(prefixes: tuple[str, ...] = ("ko", "en")) -> None:
    try:
        import edge_tts
    except ImportError:
        raise SystemExit("edge-tts가 필요합니다: pip install edge-tts")
    voices = await edge_tts.list_voices()
    for prefix in prefixes:
        matching = [v for v in voices if v["Locale"].startswith(prefix)]
        print(f"\n{prefix} 음성 {len(matching)}개:")
        for v in sorted(matching, key=lambda x: x["ShortName"]):
            name = v["ShortName"]
            gender = v["Gender"]
            friendly = v.get("FriendlyName", "")
            print(f"  {name:42s} {gender:6s} {friendly}")


def main() -> int:
    p = argparse.ArgumentParser(
        description="텍스트/PDF → MP3 변환 (Microsoft Edge TTS, 무료, 한국어 신경망 음성)"
    )
    p.add_argument(
        "input",
        nargs="?",
        help="입력 파일 (.txt / .md / .pdf)",
    )
    p.add_argument(
        "--output",
        "-o",
        default=None,
        help="출력 mp3 경로 (기본: 입력과 같은 이름의 .mp3)",
    )
    p.add_argument(
        "--ko-voice",
        default=DEFAULT_KO_VOICE,
        help=f"한국어 문장용 음성 (기본: {DEFAULT_KO_VOICE})",
    )
    p.add_argument(
        "--en-voice",
        default=None,
        help=(
            "영어 문장용 음성 ShortName 직접 지정. "
            "--en-accent보다 우선. 미지정 시 --en-accent 사용."
        ),
    )
    p.add_argument(
        "--en-accent",
        default="us",
        choices=sorted(ACCENT_PRESETS.keys()),
        help=(
            "영어 액센트 단축키 (기본: us). "
            "us/gb/au/in/ie/ca/nz/sg/jp/ko 각각에 -m 붙이면 남성. "
            "jp/ko는 해당 언어 음성으로 영어 발음 (외국인 영어 시뮬레이션)"
        ),
    )
    p.add_argument(
        "--voice",
        default=None,
        help=(
            "단일 음성으로 모든 텍스트 처리 (언어 전환 비활성). "
            "예: ko-KR-HyunsuMultilingualNeural — 한 음성이 한·영 둘 다 원어민급"
        ),
    )
    p.add_argument(
        "--rate",
        default="+0%",
        help="발화 속도. +50% (1.5배), -20% (느림), +0% (기본)",
    )
    p.add_argument(
        "--ocr",
        choices=["never", "auto", "always"],
        default="auto",
        help="OCR 정책 (PDF 한정, 기본: auto = 텍스트 추출 빈약할 때만)",
    )
    p.add_argument(
        "--elsa",
        action="store_true",
        help="❄️ 엘사 모드 활성화 (청아하고 우아한 겨울왕국 스타일 음성) ❄️",
    )
    p.add_argument(
        "--sherlock",
        action="store_true",
        help="🎻 셜록 모드 활성화 (베네딕트 컴버비치 스타일의 깊고 묵직한 영국 남성 저음) 🎻",
    )
    p.add_argument(
        "--list-voices",
        action="store_true",
        help="한국어/영어 음성 목록 출력 후 종료",
    )
    args = p.parse_args()

    if args.list_voices:
        asyncio.run(list_available_voices())
        return 0

    if not args.input:
        p.print_help()
        return 1

    input_path = Path(args.input)
    if not input_path.exists():
        raise SystemExit(f"파일 없음: {input_path}")

    output_path = (
        Path(args.output) if args.output else input_path.with_suffix(".mp3")
    )

    if args.elsa:
        ko_voice = "ko-KR-SunHiNeural"
        en_voice = "en-US-EmmaNeural"
        pitch = "+15Hz"
        mode_desc = "❄️ 엘사 모드 (한국어: SunHi +15Hz / 영어: Emma +15Hz) ❄️"
    elif args.sherlock:
        ko_voice = "ko-KR-InJoonNeural"
        en_voice = "en-GB-RyanNeural"
        pitch = "-15Hz"
        mode_desc = "🎻 셜록 모드 (한국어: InJoon / 영어: Ryan -15Hz 컴버비치 저음) 🎻"
    elif args.voice:
        ko_voice = args.voice
        en_voice = None  # 단일 음성 모드
        pitch = "+0Hz"
        mode_desc = f"단일 음성 ({args.voice})"
    else:
        ko_voice = args.ko_voice
        # 우선순위: --en-voice 명시 > --en-accent
        en_voice = args.en_voice or ACCENT_PRESETS[args.en_accent]
        accent_note = (
            f" (en-accent={args.en_accent})" if not args.en_voice else ""
        )
        pitch = "+0Hz"
        mode_desc = f"한국어={ko_voice} / 영어={en_voice}{accent_note}"

    print(f"입력: {input_path}")
    print(f"출력: {output_path}")
    print(f"음성: {mode_desc}, 속도: {args.rate}")
    print()

    text = read_input(input_path, ocr_mode=args.ocr)
    if not text.strip():
        raise SystemExit("추출된 텍스트가 비어 있습니다.")

    print(f"텍스트 길이: {len(text):,}자")
    asyncio.run(synthesize(text, ko_voice, en_voice, args.rate, pitch, output_path))
    print(f"\n완료: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
