"""Supabase Storage → Cloudflare R2 마이그레이션.

1. audio/corners/optimized/ MP3 → R2 업로드
2. artwork/ 로컬 JPEG → R2 업로드
3. 로컬 파일 없는 아트워크 → Imagen 4로 재생성 후 R2 업로드
4. supabase_urls.json, artwork_urls.json → R2 URL로 갱신
5. build_player.py 재빌드

실행 전 .env에 R2 환경변수 필요:
  R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
  R2_BUCKET_NAME, R2_PUBLIC_URL

사용:
  python tools/migrate_to_r2.py                  # 전체 (재생성 포함)
  python tools/migrate_to_r2.py --skip-regen     # 로컬 파일만 업로드 (재생성 안 함)
  python tools/migrate_to_r2.py --dry-run        # 확인만
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

# .env 로드
try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT / ".env")
except ImportError:
    env_path = PROJECT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

import build_player as bp
import generate_artwork as ga


def get_r2_client():
    try:
        import boto3
    except ImportError:
        raise SystemExit("boto3 필요: pip install boto3")

    account_id = os.environ.get("R2_ACCOUNT_ID")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")

    if not all([account_id, access_key, secret_key]):
        raise SystemExit(
            ".env에 R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY 필요.\n"
            "SUPABASE_TO_R2_MIGRATION.md 를 참고해 R2 버킷과 API 토큰을 먼저 생성하세요."
        )

    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )


def r2_upload(client, local_path: Path, r2_key: str, content_type: str,
              bucket: str, public_base: str, dry_run: bool = False) -> str:
    """파일 하나를 R2에 업로드. 이미 있으면 스킵. URL 반환."""
    url = f"{public_base}/{r2_key}"
    if dry_run:
        print(f"  [DRY] {r2_key}")
        return url
    try:
        client.head_object(Bucket=bucket, Key=r2_key)
        print(f"  [EXISTS] {r2_key}")
        return url
    except Exception:
        pass
    print(f"  [UPLOAD] {r2_key} ... ", end="", flush=True)
    client.upload_file(str(local_path), bucket, r2_key,
                       ExtraArgs={"ContentType": content_type})
    print("OK")
    return url


def r2_upload_bytes(client, data: bytes, r2_key: str, content_type: str,
                    bucket: str, public_base: str, dry_run: bool = False) -> str:
    url = f"{public_base}/{r2_key}"
    if dry_run:
        print(f"  [DRY] {r2_key}")
        return url
    print(f"  [UPLOAD] {r2_key} ... ", end="", flush=True)
    client.upload_fileobj(io.BytesIO(data), bucket, r2_key,
                          ExtraArgs={"ContentType": content_type})
    print("OK")
    return url


def main() -> int:
    p = argparse.ArgumentParser(description="Supabase → Cloudflare R2 마이그레이션")
    p.add_argument("--skip-regen", action="store_true",
                   help="로컬 파일 없는 아트워크 재생성 건너뜀")
    p.add_argument("--dry-run", action="store_true",
                   help="업로드 없이 대상 목록만 출력")
    p.add_argument("--no-build", action="store_true",
                   help="마이그레이션 후 build_player.py 재빌드 생략")
    args = p.parse_args()

    bucket = os.environ.get("R2_BUCKET_NAME", "gmp-audio")
    public_base = os.environ.get("R2_PUBLIC_URL", "").rstrip("/")

    if not public_base:
        raise SystemExit(".env에 R2_PUBLIC_URL 필요 (예: https://pub-XXXX.r2.dev)")

    client = None if args.dry_run else get_r2_client()

    # ── STEP 1: MP3 업로드 ──────────────────────────────────────────────────
    optimized_dir = PROJECT / "audio" / "corners" / "optimized"
    mp3_urls: dict[str, str] = {}

    if optimized_dir.exists():
        mp3s = sorted(optimized_dir.glob("*.mp3"))
        print(f"\n[MP3] {len(mp3s)}개 업로드 대상")
        for mp3 in mp3s:
            url = r2_upload(client, mp3, mp3.name, "audio/mpeg",
                            bucket, public_base, args.dry_run)
            mp3_urls[mp3.name] = url
        if not args.dry_run:
            urls_path = PROJECT / "supabase_urls.json"
            urls_path.write_text(
                json.dumps(mp3_urls, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"supabase_urls.json 갱신 ({len(mp3_urls)}개)")
    else:
        print(f"\n[MP3] {optimized_dir} 없음 — 스킵")

    # ── STEP 2: 아트워크 업로드 + 재생성 ────────────────────────────────────
    artwork_dir = PROJECT / "artwork"
    artwork_urls_path = PROJECT / "artwork_urls.json"
    existing_artwork_urls = ga.load_artwork_urls(artwork_urls_path)

    # corners/ 의 모든 SRT → 필요한 ep_id 목록
    corners_dir = PROJECT / "audio" / "corners"
    all_ep_ids: list[tuple[str, str, str]] = []  # (ep_id, date_str, corner_id)
    if corners_dir.exists():
        for srt in sorted(corners_dir.glob("*.srt")):
            parts = srt.stem.split("_", 1)
            if len(parts) == 2:
                all_ep_ids.append((srt.stem, parts[0], parts[1]))

    print(f"\n[ARTWORK] 처리 대상: {len(all_ep_ids)}개 에피소드")

    new_artwork_urls: dict[str, str] = {}
    gemini_api_key = (
        os.environ.get("gemini_api_key")
        or os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
    )

    regen_count = 0
    for ep_id, date_str, corner_id in all_ep_ids:
        local_jpg = artwork_dir / f"{ep_id}.jpg"

        if local_jpg.exists():
            # 로컬 파일 있음 → 바로 업로드
            r2_key = f"artwork/{ep_id}.jpg"
            url = r2_upload(client, local_jpg, r2_key, "image/jpeg",
                            bucket, public_base, args.dry_run)
            new_artwork_urls[ep_id] = url

        elif not args.skip_regen and gemini_api_key:
            # 로컬 없음 → Imagen으로 재생성
            song_info = ga.extract_song_info(
                bp.find_original_title(PROJECT / "audio", date_str, corner_id)
            )
            if not song_info:
                print(f"  [SKIP] {ep_id} (곡 정보 없음)")
                continue

            print(f"  [REGEN] {ep_id} — {song_info[0]} - {song_info[1]}")
            if args.dry_run:
                new_artwork_urls[ep_id] = f"{public_base}/artwork/{ep_id}.jpg"
                continue

            try:
                prompt = ga.build_prompt(song_info[0], song_info[1])
                png_bytes = ga.generate_image(gemini_api_key, prompt)
                jpg_bytes = ga.compress_to_jpeg(png_bytes)

                # 로컬 저장
                artwork_dir.mkdir(exist_ok=True)
                local_jpg.write_bytes(jpg_bytes)

                # R2 업로드
                r2_key = f"artwork/{ep_id}.jpg"
                url = r2_upload_bytes(client, jpg_bytes, r2_key, "image/jpeg",
                                      bucket, public_base, args.dry_run)
                new_artwork_urls[ep_id] = url
                regen_count += 1

                # Rate limit 방지
                time.sleep(4)

            except Exception as e:
                print(f"  [ERROR] {ep_id}: {e}")
                if "RESOURCE_EXHAUSTED" in str(e) or "429" in str(e):
                    print("  Rate limit — 60초 대기...")
                    time.sleep(60)
        else:
            if args.skip_regen:
                print(f"  [SKIP] {ep_id} (로컬 파일 없음, --skip-regen)")
            else:
                print(f"  [SKIP] {ep_id} (로컬 파일 없음, gemini_api_key 없음)")

    if not args.dry_run and new_artwork_urls:
        ga.save_artwork_urls(new_artwork_urls, artwork_urls_path)
        print(f"\nartwork_urls.json 갱신 ({len(new_artwork_urls)}개)")

    # ── STEP 3: 플레이어 재빌드 ─────────────────────────────────────────────
    if not args.no_build and not args.dry_run and (mp3_urls or new_artwork_urls):
        print("\n[BUILD] 플레이어 데이터 재빌드...")
        url_map = bp.load_url_map(PROJECT / "supabase_urls.json")
        movie_map = bp.load_movie_mapping(PROJECT / "movie_mapping.json")
        artwork_map = ga.load_artwork_urls(artwork_urls_path)

        data_dir = PROJECT / "data"
        data_dir.mkdir(exist_ok=True)

        all_episodes = []
        if corners_dir.exists():
            for srt in sorted(corners_dir.glob("*.srt")):
                e = bp.build_episode(srt, PROJECT / "audio", url_map, movie_map, artwork_map)
                if e:
                    all_episodes.append(e)

        bp.write_index_json(all_episodes, data_dir)
        for ep in all_episodes:
            bp.write_episode_json(ep, data_dir)
        print(f"  {len(all_episodes)}개 에피소드 빌드 완료")
        print(f"\n다음 단계: git add data/ && git commit -m 'migrate: Supabase → R2' && git push")

    print(f"\n=== 마이그레이션 완료 ===")
    print(f"  MP3: {len(mp3_urls)}개")
    print(f"  아트워크: {len(new_artwork_urls)}개 (재생성: {regen_count}개)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
