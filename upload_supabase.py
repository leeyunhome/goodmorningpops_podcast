"""Cloudflare R2에 MP3 업로드 + public URL 수집.

설정 (.env):
  R2_ACCOUNT_ID=f5280adda1535bcb64e9ae4f9d49f8fb
  R2_ACCESS_KEY_ID=<Access Key ID>
  R2_SECRET_ACCESS_KEY=<Secret Access Key>
  R2_BUCKET_NAME=gmp-audio
  R2_PUBLIC_URL=https://pub-XXXX.r2.dev

사용:
  python upload_supabase.py audio/corners/optimized
  python upload_supabase.py audio/corners/optimized --overwrite

산출: supabase_urls.json 에 {파일명: public_url} 누적 저장.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

CORNER_FILENAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}_(?:screen_english|review_time|review_time_screen|"
    r"friday_news_pick|laura_scrapbook|pop_song)\.mp3$"
)

URLS_FILE = Path("supabase_urls.json")


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


def get_client():
    try:
        import boto3
    except ImportError:
        raise SystemExit("boto3 패키지가 필요합니다: pip install boto3")

    account_id = os.environ.get("R2_ACCOUNT_ID")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")

    if not all([account_id, access_key, secret_key]):
        raise SystemExit(
            ".env에 R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY 필요.\n"
            "SUPABASE_TO_R2_MIGRATION.md 참고."
        )

    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )


def load_urls() -> dict:
    if URLS_FILE.exists():
        try:
            return json.loads(URLS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_urls(mapping: dict) -> None:
    URLS_FILE.write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def get_public_url(filename: str) -> str:
    return load_urls().get(filename, "")


def upload_one(file_path: Path, overwrite: bool = False, allow_full: bool = False) -> str:
    """MP3 한 개를 R2에 업로드하고 public URL 반환. 이미 있으면 기존 URL 반환."""
    name = file_path.name
    if not allow_full and not CORNER_FILENAME_RE.match(name):
        print(f"  [SKIP] 안전 필터: {name}")
        return ""

    bucket = os.environ.get("R2_BUCKET_NAME", "gmp-audio")
    public_base = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")

    if not public_base:
        raise SystemExit(".env에 R2_PUBLIC_URL 필요 (예: https://pub-XXXX.r2.dev)")

    # 이미 URL 매핑에 있으면 스킵 (overwrite 아닌 경우)
    if not overwrite:
        existing = get_public_url(name)
        if existing:
            print(f"  [EXISTS] {name}")
            return existing

    client = get_client()

    # R2에 이미 존재하는지 확인
    if not overwrite:
        try:
            client.head_object(Bucket=bucket, Key=name)
            url = f"{public_base}/{name}"
            mapping = load_urls()
            mapping[name] = url
            save_urls(mapping)
            print(f"  [EXISTS] {name}")
            return url
        except Exception:
            pass

    print(f"  [UPLOAD] {name} ...", end=" ", flush=True)
    client.upload_file(
        str(file_path), bucket, name,
        ExtraArgs={"ContentType": "audio/mpeg"},
    )
    url = f"{public_base}/{name}"
    print(f"OK")

    mapping = load_urls()
    mapping[name] = url
    save_urls(mapping)
    return url


def main() -> int:
    p = argparse.ArgumentParser(description="MP3를 Cloudflare R2에 업로드 + URL 수집")
    p.add_argument("input", help="업로드할 폴더")
    p.add_argument("--overwrite", action="store_true", help="이미 있어도 덮어쓰기")
    p.add_argument("--dry-run", action="store_true", help="대상 목록만 출력")
    p.add_argument("--allow-full", action="store_true", help="코너 패턴 필터 비활성")
    args = p.parse_args()

    load_env()

    input_dir = Path(args.input)
    if not input_dir.is_dir():
        raise SystemExit(f"폴더가 아닙니다: {input_dir}")

    all_mp3 = sorted(input_dir.glob("*.mp3"))
    if not all_mp3:
        raise SystemExit("mp3 파일이 없습니다.")

    if args.allow_full:
        files = all_mp3
    else:
        files = [p for p in all_mp3 if CORNER_FILENAME_RE.match(p.name)]
        rejected = [p for p in all_mp3 if not CORNER_FILENAME_RE.match(p.name)]
        if rejected:
            print(f"안전 필터: {len(rejected)}개 제외 (--allow-full 로 강제 가능)")

    print(f"대상: {len(files)}개  bucket: {os.environ.get('R2_BUCKET_NAME','gmp-audio')}")

    if args.dry_run:
        for f in files:
            print(f"  {f.name}")
        return 0

    counts = {"uploaded": 0, "exists": 0, "error": 0}
    for i, f in enumerate(files, 1):
        try:
            before = get_public_url(f.name)
            url = upload_one(f, args.overwrite)
            after = get_public_url(f.name)
            if url and before != after:
                counts["uploaded"] += 1
            elif url:
                counts["exists"] += 1
        except Exception as e:
            counts["error"] += 1
            print(f"  [{i}/{len(files)}] ERROR {f.name}: {e}")

    print(f"\n완료: 신규 {counts['uploaded']}  기존 {counts['exists']}  에러 {counts['error']}")
    print(f"URL 매핑: {URLS_FILE}  (총 {len(load_urls())}개)")
    return 0


if __name__ == "__main__":
    load_env()
    sys.exit(main())
