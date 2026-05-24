"""회차별 스트리밍 파이프라인:
   download → transcribe → extract → optimize → upload → build → git push

각 회차가 끝날 때마다 Supabase 업로드 + GitHub Pages push가 즉시 일어나므로,
긴 기간을 돌릴 때 중간 결과를 바로 라이브로 볼 수 있다.

핵심 원칙:
- 커버리지 우선: 한 회차 실패해도 다음 회차로 계속.
- 모델/클라이언트는 한 번만 로딩 후 재사용.
- 모든 단계는 idempotent: 이미 처리된 산출물은 자동 스킵.

사용:
  python run_pipeline.py 1494088127 --from 2020-06-01 --to 2023-10-31 ^
    --repo ../goodmorningpops_podcast --prompt "조정현, 굿모닝 팝스, KBS"

GitHub Pages repo는 별도 폴더에 미리 clone 되어 있어야 한다 (`--repo`).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import traceback
from datetime import datetime
from pathlib import Path

# Windows: torch lib을 PATH에 등록해야 ctranslate2가 cuDNN 9를 찾음 (LESSONS_LEARNED.md 참조)
if sys.platform == "win32":
    try:
        import torch  # noqa: F401
        _torch_lib = os.path.join(os.path.dirname(torch.__file__), "lib")
        if os.path.isdir(_torch_lib):
            os.add_dll_directory(_torch_lib)
            os.environ["PATH"] = _torch_lib + os.pathsep + os.environ.get("PATH", "")
    except ImportError:
        pass

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

import build_player as bp
import extract_corner as ec
import fetch_episode as fe
import optimize_mp3 as om
import transcribe as tr
import upload_supabase as us
from faster_whisper import WhisperModel


def backup_corners(audio_dir: Path) -> Path | None:
    """audio/corners → audio/corners_backup_YYYYMMDD_HHMMSS 로 이름만 변경."""
    corners = audio_dir / "corners"
    if not corners.is_dir():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = audio_dir / f"corners_backup_{timestamp}"
    shutil.move(str(corners), str(backup))
    print(f"기존 corners 백업: {backup.name}")
    return backup


def git_run(args: list[str], cwd: Path) -> tuple[int, str, str]:
    res = subprocess.run(args, cwd=str(cwd), capture_output=True)
    return (
        res.returncode,
        res.stdout.decode("utf-8", errors="replace"),
        res.stderr.decode("utf-8", errors="replace"),
    )


def git_push(repo_path: Path, message: str) -> str:
    """git add . && commit && push. 반환: 'pushed' / 'nothing' / 'error: ...'"""
    code, _, err = git_run(["git", "add", "."], repo_path)
    if code != 0:
        return f"error(add): {err.strip()[:200]}"
    code, out, err = git_run(["git", "commit", "-m", message], repo_path)
    if code != 0:
        combined = (out + err).lower()
        if "nothing to commit" in combined or "no changes added" in combined:
            return "nothing"
        return f"error(commit): {(err or out).strip()[:200]}"
    code, _, err = git_run(["git", "push"], repo_path)
    if code != 0:
        return f"error(push): {err.strip()[:200]}"
    return "pushed"


def process_episode(
    ep: dict,
    *,
    model: WhisperModel,
    sb_storage,
    audio_dir: Path,
    out_corners: Path,
    pages_repo: Path,
    web_dir: Path,
    movie_map: dict,
    url_map_path: Path,
    args,
) -> str:
    """한 회차의 전체 파이프라인을 수행하고 결과 상태 문자열을 반환."""
    d = ep["date"]
    date_str = d.strftime("%Y-%m-%d")

    # 1. Download
    dest_mp3 = fe.make_dest(audio_dir, ep)
    if not dest_mp3.exists():
        print(f"  [1/7] download...")
        fe.download(ep["url"], dest_mp3)
    else:
        print(f"  [1/7] mp3 이미 있음")

    # Screen English나 Review Time이 아니면 여기서 종료
    name_lower = dest_mp3.name.lower()
    if "screen english" not in name_lower and "review time" not in name_lower:
        return "not-screen-english"

    # 2. Transcribe
    srt_path = dest_mp3.with_suffix(".srt")
    txt_path = dest_mp3.with_suffix(".txt")
    md_path = dest_mp3.with_suffix(".md")
    if not all(p.exists() for p in [srt_path, txt_path, md_path]):
        print(f"  [2/7] transcribe...")
        tr.transcribe_one(
            model=model,
            audio_path=dest_mp3,
            language="ko",
            beam_size=5,
            vad=True,
            initial_prompt=args.prompt,
            overwrite=False,
            formats={"txt", "srt", "md"},
        )
    else:
        print(f"  [2/7] 전사 이미 있음")

    # 3. Extract Screen English or Review Time
    corner_id = "review_time_screen" if "review time" in name_lower else "screen_english"
    corner_def = ec.CORNER_DEFS[corner_id]
    print(f"  [3/7] extract...")
    out_corners.mkdir(parents=True, exist_ok=True)
    ok = ec.process_one(
        audio_path=dest_mp3,
        corner=corner_def,
        corner_id=corner_id,
        out_dir=out_corners,
        manual_start=None,
        manual_end=None,
        overwrite=args.overwrite,
        dry_run=False,
    )
    if not ok:
        return "extract-skipped"

    corner_mp3 = out_corners / f"{date_str}_{corner_id}.mp3"
    if not corner_mp3.exists():
        return "extract-no-output"

    # 4. Optimize
    optimized_dir = out_corners / "optimized"
    optimized_mp3 = optimized_dir / corner_mp3.name
    if not optimized_mp3.exists() or args.overwrite:
        print(f"  [4/7] optimize (64k mono)...")
        om.reencode(corner_mp3, optimized_mp3, args.bitrate, mono=True)
    else:
        print(f"  [4/7] 최적화본 이미 있음")

    # 5. Upload to Supabase
    print(f"  [5/7] upload supabase...")
    action = us.upload_one(
        sb_storage, optimized_mp3, optimized_mp3.name, args.overwrite
    )
    public_url = sb_storage.get_public_url(optimized_mp3.name)

    # URL 매핑 누적
    url_map = bp.load_url_map(url_map_path)
    url_map[optimized_mp3.name] = public_url
    url_map_path.write_text(
        json.dumps(url_map, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 6. Build player JSON (이 회차 + index 재생성)
    print(f"  [6/7] build player data...")
    data_dir = pages_repo / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    corner_srt = out_corners / f"{date_str}_{corner_id}.srt"
    ep_obj = bp.build_episode(corner_srt, audio_dir, url_map, movie_map)
    if ep_obj is None:
        return "build-failed"
    bp.write_episode_json(ep_obj, data_dir)

    # index.json: 현재까지 corners/ 에 있는 모든 srt 기준으로 재생성
    all_episodes = []
    for srt in sorted(out_corners.glob("*.srt")):
        e = bp.build_episode(srt, audio_dir, url_map, movie_map)
        if e:
            all_episodes.append(e)
    bp.write_index_json(all_episodes, data_dir)
    bp.copy_static_assets(web_dir, pages_repo)

    # 7. Git push
    if args.no_push:
        print(f"  [7/7] git push 스킵 (--no-push)")
        return "built"

    print(f"  [7/7] git push...")
    result = git_push(pages_repo, f"deploy: {date_str} screen_english")
    if result == "pushed":
        return "deployed"
    if result == "nothing":
        return "deployed(no-change)"
    return result


def main() -> int:
    p = argparse.ArgumentParser(
        description="회차별 스트리밍 파이프라인 (Screen English 추출 → 배포)"
    )
    p.add_argument("apple_id", help="Apple Podcasts ID (예: 1494088127)")
    p.add_argument(
        "--from", dest="from_date", required=True, help="시작 날짜 YYYY-MM-DD"
    )
    p.add_argument(
        "--to", dest="to_date", required=True, help="종료 날짜 YYYY-MM-DD"
    )
    p.add_argument(
        "--repo",
        required=True,
        help="GitHub Pages 배포 repo 로컬 경로 (미리 clone 되어 있어야 함)",
    )
    p.add_argument("--audio-dir", default="audio", help="audio 폴더 (기본: audio)")
    p.add_argument("--web-dir", default="web", help="정적 자산 폴더 (기본: web)")
    p.add_argument(
        "--model", default="large-v3-turbo", help="Whisper 모델 (기본: large-v3-turbo)"
    )
    p.add_argument(
        "--device",
        choices=["auto", "cpu", "cuda"],
        default="auto",
        help="추론 디바이스 (기본: auto)",
    )
    p.add_argument(
        "--prompt", default=None, help="전사 초기 프롬프트 (고유명사 표기)"
    )
    p.add_argument(
        "--bitrate", default="64k", help="MP3 재인코딩 비트레이트 (기본: 64k)"
    )
    p.add_argument(
        "--overwrite",
        action="store_true",
        help="추출/최적화/업로드를 강제 갱신",
    )
    p.add_argument(
        "--backup-corners",
        action="store_true",
        help="시작 전 audio/corners/ 를 timestamped 폴더로 백업 (이름 변경)",
    )
    p.add_argument(
        "--no-push",
        action="store_true",
        help="git push 생략 (테스트용, 빌드까지만)",
    )
    p.add_argument(
        "--stop-on-error",
        action="store_true",
        help="에러 발생 시 중단 (기본: 다음 회차로 계속, 커버리지 우선)",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="처리 최대 회차 수 (테스트용)",
    )
    args = p.parse_args()

    audio_dir = Path(args.audio_dir)
    pages_repo = Path(args.repo)
    web_dir = Path(args.web_dir)
    url_map_path = Path("supabase_urls.json")
    movie_map_path = Path("movie_mapping.json")

    if not pages_repo.is_dir():
        raise SystemExit(f"--repo 경로가 폴더가 아닙니다: {pages_repo}")
    if not (pages_repo / ".git").is_dir() and not args.no_push:
        print(
            f"⚠ {pages_repo} 가 git repo가 아닌 듯합니다. --no-push 로 진행하거나 "
            f"먼저 git clone 하세요."
        )

    # 피드 + 날짜 필터
    print(f"피드 조회: id={args.apple_id}")
    feed_url = fe.lookup_feed_url(args.apple_id)
    all_eps = fe.list_episodes(feed_url, prefer="title")

    from_date = fe.parse_date(args.from_date)
    to_date = fe.parse_date(args.to_date)
    if from_date > to_date:
        raise SystemExit("--from 이 --to 보다 늦습니다.")

    targets = []
    for ep in all_eps:
        d = ep.get("date")
        if d is None or d < from_date or d > to_date:
            continue
        targets.append(ep)
    targets.sort(key=lambda e: e["date"])
    if args.limit:
        targets = targets[: args.limit]
    print(f"기간 내 회차: {len(targets)}개\n")

    if not targets:
        return 1

    # 백업
    if args.backup_corners:
        backup_corners(audio_dir)

    out_corners = audio_dir / "corners"
    out_corners.mkdir(parents=True, exist_ok=True)

    # Whisper 모델 로딩 (한 번만)
    print("Whisper 모델 로딩...")
    device, compute_type = tr.detect_device(args.device)
    print(f"  디바이스: {device} ({compute_type}), 모델: {args.model}")
    model = WhisperModel(args.model, device=device, compute_type=compute_type)

    # Supabase 클라이언트
    print("Supabase 연결...")
    us.load_env()
    bucket = os.environ.get("SUPABASE_BUCKET") or "gmp-audio"
    sb_client = us.get_client()
    sb_storage = sb_client.storage.from_(bucket)
    print(f"  bucket: {bucket}")

    # 영화 매핑
    movie_map = bp.load_movie_mapping(movie_map_path)
    print(f"  영화 매핑: {len(movie_map)}개\n")

    # 스트리밍 처리
    stats: dict[str, int] = {}
    started = datetime.now()
    for i, ep in enumerate(targets, 1):
        d = ep["date"]
        date_str = d.strftime("%Y-%m-%d")
        title = ep.get("title", "")[:70]
        print(f"\n[{i}/{len(targets)}] {date_str}  {title}")
        try:
            status = process_episode(
                ep,
                model=model,
                sb_storage=sb_storage,
                audio_dir=audio_dir,
                out_corners=out_corners,
                pages_repo=pages_repo,
                web_dir=web_dir,
                movie_map=movie_map,
                url_map_path=url_map_path,
                args=args,
            )
        except Exception as e:
            status = f"error: {type(e).__name__}: {str(e)[:200]}"
            if args.stop_on_error:
                print(f"  ✗ {status}")
                traceback.print_exc()
                stats[status] = stats.get(status, 0) + 1
                break
            print(f"  ✗ {status}")
            traceback.print_exc()
        stats[status] = stats.get(status, 0) + 1
        print(f"  → {status}")

    elapsed = datetime.now() - started
    print(f"\n=== 완료 (경과 {elapsed}) ===")
    for k, v in sorted(stats.items(), key=lambda x: -x[1]):
        print(f"  {v:3d}  {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
