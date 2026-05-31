"""Supabase Storage에 mp3 일괄 업로드 + public URL 수집.

설정 (.env):
  SUPABASE_URL=https://xxxxx.supabase.co
  SUPABASE_KEY=eyJ...                # anon public key
  SUPABASE_BUCKET=gmp-audio          # 본인이 만든 public bucket 이름

사용:
  python upload_supabase.py audio/corners/optimized
  python upload_supabase.py audio/corners/optimized --bucket gmp-audio
  python upload_supabase.py audio/corners/optimized --overwrite

산출: 업로드된 파일의 (파일명 -> public URL) 매핑이 supabase_urls.json 에 누적 저장.
이후 build_player.py 가 이걸 읽어 player JSON 에 audio_url 을 넣는다.
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

# 코너 추출본만 허용. 본 방송 mp3는 파일명이 이 패턴과 다르므로 자동 거부됨.
# 형식: YYYY-MM-DD_<코너id>.mp3
CORNER_FILENAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}_(?:screen_english|review_time|review_time_screen|"
    r"friday_news_pick|laura_scrapbook|pop_song)\.mp3$"
)


def load_env(env_path: Path = Path(".env")) -> None:
    """python-dotenv가 있으면 사용, 없으면 간단 파서."""
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
        from supabase import create_client
    except ImportError:
        raise SystemExit("supabase 패키지가 필요합니다: pip install supabase")

    url = os.environ.get("SUPABASE_URL")
    # 업로드에는 service_role 키가 필요 (anon 키는 Storage RLS에 막힘)
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_KEY")
    if not url or not key:
        raise SystemExit(
            ".env 파일 또는 환경변수에 SUPABASE_URL, SUPABASE_SERVICE_KEY 가 필요합니다.\n"
            "Supabase 프로젝트 > Project Settings > API 에서 확인."
        )
    return create_client(url, key)


def upload_one(
    storage, file_path: Path, remote_name: str, overwrite: bool
) -> str:
    """단일 파일 업로드. 'uploaded' / 'exists' 반환."""
    with open(file_path, "rb") as f:
        data = f.read()
    file_options = {
        "content-type": "audio/mpeg",
        # supabase-py v2: upsert는 문자열 'true'/'false'
        "upsert": "true" if overwrite else "false",
    }
    try:
        storage.upload(remote_name, data, file_options=file_options)
        return "uploaded"
    except Exception as e:
        msg = str(e).lower()
        # 이미 존재 (overwrite=False였을 때)
        if "duplicate" in msg or "already exists" in msg or "the resource already" in msg:
            return "exists"
        raise


def main() -> int:
    p = argparse.ArgumentParser(
        description="mp3를 Supabase Storage public bucket 으로 업로드 + URL 수집"
    )
    p.add_argument("input", help="업로드할 폴더 (mp3들)")
    p.add_argument(
        "--bucket",
        default=None,
        help="Storage bucket 이름 (기본: $SUPABASE_BUCKET 또는 'gmp-audio')",
    )
    p.add_argument(
        "--out",
        default="supabase_urls.json",
        help="파일명 -> URL 매핑 누적 저장 JSON (기본: supabase_urls.json)",
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="이미 있는 파일도 덮어쓰기",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="업로드 안 하고 대상 목록만 출력",
    )
    p.add_argument(
        "--allow-full",
        action="store_true",
        help=(
            "안전 필터 비활성 — 코너 추출본이 아닌 파일도 업로드 허용. "
            "기본은 'YYYY-MM-DD_<코너>.mp3' 패턴만 통과."
        ),
    )
    args = p.parse_args()

    load_env()

    bucket = args.bucket or os.environ.get("SUPABASE_BUCKET") or "gmp-audio"

    input_dir = Path(args.input)
    if not input_dir.is_dir():
        raise SystemExit(f"폴더가 아닙니다: {input_dir}")

    all_mp3 = sorted(input_dir.glob("*.mp3"))
    if not all_mp3:
        raise SystemExit("mp3 파일이 없습니다.")

    if args.allow_full:
        files = all_mp3
        rejected: list[Path] = []
    else:
        files = [p for p in all_mp3 if CORNER_FILENAME_RE.match(p.name)]
        rejected = [p for p in all_mp3 if not CORNER_FILENAME_RE.match(p.name)]

    if rejected:
        print(
            f"⚠ 안전 필터: {len(rejected)}개 파일이 코너 추출본 패턴과 달라 "
            f"업로드에서 제외됩니다."
        )
        for p in rejected[:5]:
            print(f"  거부: {p.name}")
        if len(rejected) > 5:
            print(f"  ... 외 {len(rejected) - 5}개")
        print("  (본 방송 mp3로 보임. 정말 올리려면 --allow-full)\n")

    if not files:
        raise SystemExit(
            "업로드할 파일이 없습니다.\n"
            "코너 추출본 파일명은 'YYYY-MM-DD_screen_english.mp3' 형식이어야 합니다.\n"
            "본 방송 mp3를 의도적으로 올리려면 --allow-full 옵션을 추가하세요."
        )

    print(f"대상 {len(files)}개")
    print(f"Supabase bucket: {bucket}")
    for f in files[:10]:
        print(f"  {f.name}")
    if len(files) > 10:
        print(f"  ... 외 {len(files) - 10}개")

    if args.dry_run:
        print("\n--dry-run: 업로드 생략")
        return 0

    client = get_client()
    storage = client.storage.from_(bucket)

    out_path = Path(args.out)
    url_map: dict[str, str] = {}
    if out_path.exists():
        try:
            url_map = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            url_map = {}

    counts = {"uploaded": 0, "exists": 0, "error": 0}
    print()
    for i, f in enumerate(files, 1):
        remote = f.name
        try:
            action = upload_one(storage, f, remote, args.overwrite)
            url = storage.get_public_url(remote)
            url_map[remote] = url
            counts[action] += 1
            mark = "+" if action == "uploaded" else "."
            print(f"  [{i}/{len(files)}] {mark} {remote}")
        except Exception as e:
            counts["error"] += 1
            print(f"  [{i}/{len(files)}] ! {remote}: {e}")

    out_path.write_text(
        json.dumps(url_map, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print()
    print(
        f"완료: 신규 {counts['uploaded']}  /  기존 {counts['exists']}  "
        f"/  에러 {counts['error']}"
    )
    print(f"URL 매핑 저장: {out_path}  (총 {len(url_map)}개 항목)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
