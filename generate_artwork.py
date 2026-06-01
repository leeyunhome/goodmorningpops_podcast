"""곡 제목+아티스트로 수채화풍 AI 이미지를 생성한다 (Google Imagen 3).

생성된 이미지는 Supabase Storage에 업로드되고 URL이 artwork_urls.json에 저장된다.
build_player.py가 이 JSON을 읽어 episode JSON에 artwork_generated_url을 넣는다.

사용:
  python generate_artwork.py audio/corners --limit 5
  python generate_artwork.py audio/corners --date 2026-05-28
  python generate_artwork.py audio/corners --dry-run

환경변수 (.env):
  gemini_api_key=AIza...
  SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_BUCKET (업로드용)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

ARTWORK_URLS_FILE = "artwork_urls.json"
DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_")


def load_env(env_path: Path = Path(".env")) -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path)
        return
    except ImportError:
        pass
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_api_key() -> str:
    key = os.environ.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise SystemExit(
            ".env에 gemini_api_key가 필요합니다.\n"
            "Google AI Studio (https://aistudio.google.com/apikey) 에서 확인."
        )
    return key


def extract_song_info(title: str) -> tuple[str, str] | None:
    """에피소드 제목에서 (곡명, 아티스트) 추출. Screen English 등은 None."""
    if not title:
        return None
    if re.search(r"screen\s*english|review\s*time", title, re.IGNORECASE):
        return None
    # "(06/01/월) autopilot - Emily Sie" -> "autopilot", "Emily Sie"
    clean = re.sub(r"^\(\d{2}/\d{2}/[월화수목금토일]\)\s*", "", title)
    clean = clean.replace("_", "'").strip()
    if " - " in clean:
        parts = clean.split(" - ", 1)
        return parts[0].strip(), parts[1].strip()
    return clean, ""


def build_prompt(song: str, artist: str) -> str:
    return (
        f"A beautiful watercolor painting inspired by the mood of the song "
        f"'{song}' by {artist}. "
        f"Soft pastel colors, gentle brushstrokes, dreamy atmosphere. "
        f"Abstract emotional landscape with flowing watercolor washes. "
        f"No text, no letters, no words in the image. "
        f"Artistic, gallery-quality watercolor illustration."
    )


def generate_image(api_key: str, prompt: str) -> bytes:
    """Google Imagen 4 API로 이미지 생성, PNG bytes 반환."""
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        raise SystemExit(
            "google-genai 패키지가 필요합니다:\n  pip install google-genai"
        )

    client = genai.Client(api_key=api_key)

    # Imagen 4.0 시도 → 실패 시 Gemini 2.0 Flash 폴백
    for model in ["imagen-4.0-generate-001", "imagen-4.0-fast-generate-001"]:
        try:
            response = client.models.generate_images(
                model=model,
                prompt=prompt,
                config=types.GenerateImagesConfig(
                    number_of_images=1,
                    aspect_ratio="1:1",
                    safety_filter_level="BLOCK_LOW_AND_ABOVE",
                ),
            )
            if response.generated_images:
                return response.generated_images[0].image.image_bytes
        except Exception as e:
            if "404" in str(e) or "NOT_FOUND" in str(e):
                continue
            raise

    # Gemini Flash 폴백 (텍스트+이미지 멀티모달)
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"],
        ),
    )
    if response.candidates:
        for part in response.candidates[0].content.parts:
            if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                return part.inline_data.data

    raise RuntimeError("No image generated from any model")


def upload_to_supabase(image_bytes: bytes, remote_name: str) -> str:
    """Supabase Storage에 PNG 업로드, public URL 반환."""
    try:
        from supabase import create_client
    except ImportError:
        raise SystemExit("supabase 패키지 필요: pip install supabase")

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")
    bucket = os.environ.get("SUPABASE_BUCKET") or "gmp-audio"

    if not url or not key:
        raise SystemExit(".env에 SUPABASE_URL, SUPABASE_SERVICE_KEY 필요")

    client = create_client(url, key)
    storage = client.storage.from_(bucket)

    file_options = {"content-type": "image/png", "upsert": "true"}
    storage.upload(f"artwork/{remote_name}", image_bytes, file_options=file_options)
    return storage.get_public_url(f"artwork/{remote_name}")


def load_artwork_urls(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_artwork_urls(data: dict, path: Path) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    p = argparse.ArgumentParser(description="곡별 수채화 AI 이미지 생성 (Imagen 3)")
    p.add_argument("input", help="corners 폴더 (SRT 기준으로 에피소드 목록 구성)")
    p.add_argument("--date", default=None, help="특정 날짜만 (YYYY-MM-DD)")
    p.add_argument("--limit", type=int, default=None, help="최대 생성 수")
    p.add_argument("--overwrite", action="store_true", help="이미 생성된 것도 다시")
    p.add_argument("--dry-run", action="store_true", help="프롬프트만 보고 생성 안 함")
    p.add_argument("--no-upload", action="store_true", help="로컬 저장만, Supabase 스킵")
    p.add_argument("--out-dir", default="artwork", help="로컬 저장 폴더 (기본: artwork)")
    args = p.parse_args()

    load_env()
    api_key = get_api_key()

    input_dir = Path(args.input)
    if not input_dir.is_dir():
        raise SystemExit(f"폴더 아님: {input_dir}")

    urls_path = Path(ARTWORK_URLS_FILE)
    artwork_urls = load_artwork_urls(urls_path)
    out_dir = Path(args.out_dir)

    # SRT 기반으로 에피소드 목록
    srt_files = sorted(input_dir.glob("*.srt"))
    if not srt_files:
        raise SystemExit(f"SRT 없음: {input_dir}")

    targets = []
    for srt in srt_files:
        m = DATE_PREFIX_RE.match(srt.stem)
        if not m:
            continue
        date_str = m.group(1)
        if args.date and date_str != args.date:
            continue
        # 제목 추출: SRT 첫 줄이 아니라 stem에서
        corner_part = srt.stem[len(date_str) + 1:]  # e.g., "pop_song" or "screen_english"
        # 원본 mp3 제목 가져오기
        title = corner_part  # fallback
        # audio/ 에서 같은 날짜 mp3 찾기
        for mp3 in Path("audio").glob(f"{date_str}*"):
            if mp3.suffix.lower() == ".mp3":
                raw = mp3.stem
                # 날짜 접두사 제거
                raw = re.sub(r"^\d{4}-\d{2}-\d{2}_", "", raw)
                # 요일 접두사 제거
                raw = re.sub(r"^\d{1,2}_\d{1,2}_[월화수목금토일]_\s*", "", raw)
                title = raw.replace("_", "'").strip()
                break

        song_info = extract_song_info(title)
        if not song_info:
            continue
        ep_id = srt.stem
        if not args.overwrite and ep_id in artwork_urls:
            continue
        targets.append((ep_id, date_str, song_info[0], song_info[1], title))

    if args.limit:
        targets = targets[:args.limit]

    print(f"생성 대상: {len(targets)}개")
    if not targets:
        print("(이미 생성됨 또는 대상 없음)")
        return 0

    for i, (ep_id, date_str, song, artist, title) in enumerate(targets, 1):
        prompt = build_prompt(song, artist)
        print(f"\n[{i}/{len(targets)}] {date_str} — {song} - {artist}")
        print(f"  prompt: {prompt[:100]}...")

        if args.dry_run:
            continue

        try:
            print(f"  generating...")
            png_bytes = generate_image(api_key, prompt)
            print(f"  OK ({len(png_bytes) // 1024} KB)")

            # 로컬 저장
            out_dir.mkdir(parents=True, exist_ok=True)
            local_path = out_dir / f"{ep_id}.png"
            local_path.write_bytes(png_bytes)

            # Supabase 업로드
            if not args.no_upload:
                print(f"  uploading...")
                public_url = upload_to_supabase(png_bytes, f"{ep_id}.png")
                artwork_urls[ep_id] = public_url
                save_artwork_urls(artwork_urls, urls_path)
                print(f"  url: {public_url[:80]}...")
            else:
                print(f"  saved: {local_path}")

            # Rate limit (Imagen free tier: ~15/min)
            if i < len(targets):
                time.sleep(4)

        except Exception as e:
            print(f"  ERROR: {e}")
            if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
                print("  Rate limit hit, waiting 60s...")
                time.sleep(60)

    if not args.dry_run:
        save_artwork_urls(artwork_urls, urls_path)
        print(f"\n저장: {urls_path} ({len(artwork_urls)}개 항목)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
