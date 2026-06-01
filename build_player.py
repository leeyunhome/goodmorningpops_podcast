"""정적 사이트(temp_repo_podcast/)에 들어갈 자산을 생성한다.

입력:
  audio/corners/*.srt        — 추출된 코너의 자막 (extract_corner.py 산출물)
  audio/*.mp3                — 원본 회차 (제목 추출용)
  supabase_urls.json         — 파일명 -> 공개 URL 매핑 (upload_supabase.py 산출물)
  movie_mapping.json (선택)  — {"2020-06-22": "The Current War", ...}
  web/                       — HTML/CSS/JS 템플릿

출력 (temp_repo_podcast/ 안):
  data/<id>.json             — 회차별 player JSON
  data/index.json            — 전체 회차 목록
  play.html, index.html, app.js, style.css  (web/ 에서 복사)

사용:
  python build_player.py
  python build_player.py --target ../goodmorningpops-player
  python build_player.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass

CORNER_LABELS = {
    "screen_english": "Screen English",
}
ORIGINAL_TITLE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}__\d{2}_\d{2}_[월화수목금토일]_\s*(.+)\.mp3$"
)


@dataclass
class Episode:
    id: str
    date: str
    corner: str
    corner_label: str
    title: str
    movie: str
    duration_sec: float
    audio_url: str
    artwork_generated_url: str
    script: list[dict]


def parse_srt(path: Path) -> tuple[list[dict], float]:
    """SRT를 segments (list of {start, end, text}) + 총 길이로 변환."""
    text = path.read_text(encoding="utf-8")
    blocks = re.split(r"\n\s*\n", text.strip())
    out: list[dict] = []
    duration = 0.0
    for block in blocks:
        lines = block.strip().split("\n")
        if len(lines) < 3:
            continue
        try:
            timing = lines[1]
            content = "\n".join(lines[2:]).strip()
            start_s, end_s = timing.split(" --> ")
            start = _srt_time_to_sec(start_s)
            end = _srt_time_to_sec(end_s)
            out.append({"start": round(start, 2), "end": round(end, 2), "text": content})
            duration = max(duration, end)
        except (ValueError, IndexError):
            continue
    return out, duration


def _srt_time_to_sec(s: str) -> float:
    m = re.match(r"(\d{2}):(\d{2}):(\d{2}),(\d{3})", s.strip())
    if not m:
        raise ValueError(s)
    h, mi, se, ms = map(int, m.groups())
    return h * 3600 + mi * 60 + se + ms / 1000


def find_original_title(audio_dir: Path, date: str, corner: str) -> str:
    """audio/ 폴더에서 해당 날짜의 원본 mp3 제목 추출."""
    label = CORNER_LABELS.get(corner, corner)
    safe_label = label.lower()
    for p in audio_dir.iterdir():
        if not p.name.startswith(date):
            continue
        if not p.name.lower().endswith(".mp3"):
            continue
        if safe_label not in p.name.lower():
            continue
        m = ORIGINAL_TITLE_RE.match(p.name)
        if m:
            raw = m.group(1).strip()
            # "Screen English - 어쩌고" → "어쩌고"
            for prefix in ("Screen English -", "Screen English-"):
                if raw.startswith(prefix):
                    raw = raw[len(prefix):].strip()
                    break
            # 끝의 점/언더바 정리
            raw = raw.rstrip("._ ")
            # safe_name 단계에서 ' 가 _ 로 바뀌어 있을 수 있음 → 사람이 읽기 좋게
            return raw
    return ""


def load_url_map(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_movie_mapping(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def build_episode(
    srt_path: Path,
    audio_dir: Path,
    url_map: dict[str, str],
    movie_map: dict[str, str],
    artwork_map: dict[str, str] | None = None,
) -> Episode | None:
    stem = srt_path.stem  # 예: "2020-06-22_screen_english"
    m = re.match(r"^(\d{4}-\d{2}-\d{2})_(.+)$", stem)
    if not m:
        print(f"  ! 파일명 패턴 매치 안 됨: {srt_path.name}", file=sys.stderr)
        return None
    date, corner = m.group(1), m.group(2)
    corner_label = CORNER_LABELS.get(corner, corner.replace("_", " ").title())

    script, duration = parse_srt(srt_path)
    if not script:
        print(f"  ! script 비어있음: {srt_path.name}", file=sys.stderr)
        return None

    mp3_name = f"{stem}.mp3"
    audio_url = url_map.get(mp3_name, "")
    if not audio_url:
        print(f"  ! supabase URL 없음: {mp3_name} (먼저 upload_supabase.py 실행)", file=sys.stderr)

    title = find_original_title(audio_dir, date, corner)
    movie = movie_map.get(date[:7], "")

    artwork_generated_url = ""
    if artwork_map:
        artwork_generated_url = artwork_map.get(stem, "")

    return Episode(
        id=stem,
        date=date,
        corner=corner,
        corner_label=corner_label,
        title=title,
        movie=movie,
        duration_sec=round(duration, 2),
        audio_url=audio_url,
        artwork_generated_url=artwork_generated_url,
        script=script,
    )


def write_episode_json(ep: Episode, out_dir: Path) -> Path:
    out_path = out_dir / f"{ep.id}.json"
    out_path.write_text(
        json.dumps(asdict(ep), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_path


def write_index_json(episodes: list[Episode], out_dir: Path) -> Path:
    # script 본문은 빼고 메타만
    items = []
    for ep in episodes:
        d = asdict(ep)
        d.pop("script", None)
        items.append(d)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(items),
        "episodes": sorted(items, key=lambda x: x["date"], reverse=True),
    }
    out_path = out_dir / "index.json"
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_path


def copy_static_assets(src_web: Path, target: Path) -> list[Path]:
    """web/ 의 HTML/CSS/JS 를 target 루트에 복사."""
    if not src_web.is_dir():
        print(f"  ! 정적 자산 폴더 없음: {src_web} (스킵)", file=sys.stderr)
        return []
    copied = []
    for p in src_web.iterdir():
        if p.is_file() and p.suffix.lower() in {".html", ".css", ".js"}:
            dst = target / p.name
            shutil.copy2(p, dst)
            copied.append(dst)
    return copied


def main() -> int:
    p = argparse.ArgumentParser(description="player JSON + 정적 자산 빌드")
    p.add_argument(
        "--corners-dir",
        default="audio/corners",
        help="SRT 위치 (기본: audio/corners)",
    )
    p.add_argument(
        "--audio-dir",
        default="audio",
        help="원본 mp3 위치 (제목 추출용, 기본: audio)",
    )
    p.add_argument(
        "--url-map",
        default="supabase_urls.json",
        help="파일명 -> URL 매핑 JSON (기본: supabase_urls.json)",
    )
    p.add_argument(
        "--movie-map",
        default="movie_mapping.json",
        help="날짜 -> 영화명 매핑 JSON (선택, 기본: movie_mapping.json)",
    )
    p.add_argument(
        "--artwork-map",
        default="artwork_urls.json",
        help="에피소드 ID -> 생성 이미지 URL 매핑 (선택, 기본: artwork_urls.json)",
    )
    p.add_argument(
        "--web-dir",
        default="web",
        help="정적 HTML/CSS/JS 템플릿 폴더 (기본: web)",
    )
    p.add_argument(
        "--target",
        default="temp_repo_podcast",
        help="배포 저장소 경로 (기본: temp_repo_podcast)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 파일 쓰지 않고 결과만 보기",
    )
    args = p.parse_args()

    corners_dir = Path(args.corners_dir)
    audio_dir = Path(args.audio_dir)
    url_map = load_url_map(Path(args.url_map))
    movie_map = load_movie_mapping(Path(args.movie_map))
    artwork_map = load_url_map(Path(args.artwork_map))  # reuse same loader
    target = Path(args.target)
    data_dir = target / "data"

    srt_files = sorted(corners_dir.glob("*.srt"))
    if not srt_files:
        raise SystemExit(f"SRT 없음: {corners_dir}")

    print(f"SRT 입력: {len(srt_files)}개  ({corners_dir})")
    print(f"URL 매핑: {len(url_map)}개  ({args.url_map})")
    print(f"영화 매핑: {len(movie_map)}개  ({args.movie_map})")
    print(f"배포 대상: {target}")
    print()

    episodes: list[Episode] = []
    no_url: list[str] = []
    for srt in srt_files:
        ep = build_episode(srt, audio_dir, url_map, movie_map, artwork_map)
        if ep is None:
            continue
        episodes.append(ep)
        if not ep.audio_url:
            no_url.append(ep.id)

    if not episodes:
        raise SystemExit("처리 가능한 에피소드 없음.")

    print(f"\n에피소드 {len(episodes)}개 빌드 (URL 누락 {len(no_url)}개)")
    if no_url:
        print("  URL 없는 회차 (upload_supabase.py 먼저 실행 필요):")
        for x in no_url[:5]:
            print(f"    {x}")
        if len(no_url) > 5:
            print(f"    ... 외 {len(no_url)-5}개")

    if args.dry_run:
        print("\n--dry-run: 파일 쓰지 않음")
        return 0

    data_dir.mkdir(parents=True, exist_ok=True)

    for ep in episodes:
        write_episode_json(ep, data_dir)
    write_index_json(episodes, data_dir)

    copied = copy_static_assets(Path(args.web_dir), target)

    print()
    print(f"data/*.json   : {len(episodes)}개  ({data_dir})")
    print(f"data/index.json")
    print(f"정적 자산     : {len(copied)}개")
    print(f"\n완료. {target} 에서 git add/commit/push 하면 Pages 자동 배포.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
