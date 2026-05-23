"""Apple Podcasts ID로 RSS 피드를 찾아 에피소드 MP3를 다운로드한다.

날짜 단위로 필터링 가능:
  --date 2026-05-20            특정 날짜
  --from 2026-05-01 --to 2026-05-15  날짜 범위
  --index 0                    최신부터 0번 인덱스

날짜 판단 기준은 기본적으로 제목 내 방송일 표기("5월 18일", "2026-05-18" 등)를
우선 사용하고, 없으면 RSS pubDate를 사용한다. --by 로 강제 지정 가능.

--transcribe 옵션을 주면 다운로드 직후 faster-whisper로 전사도 수행한다.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import struct_time

import feedparser
import requests
from tqdm import tqdm


def lookup_feed_url(apple_id: str) -> str:
    resp = requests.get(
        "https://itunes.apple.com/lookup",
        params={"id": apple_id, "entity": "podcast"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("results"):
        raise SystemExit(f"Apple ID {apple_id}에 해당하는 팟캐스트를 찾을 수 없습니다.")
    feed_url = data["results"][0].get("feedUrl")
    if not feed_url:
        raise SystemExit("이 팟캐스트는 공개 RSS 피드를 제공하지 않습니다.")
    return feed_url


TITLE_DATE_PATTERNS = [
    re.compile(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})"),  # 2026-05-18, 2026.5.18
    re.compile(r"(?<!\d)(\d{4})(\d{2})(\d{2})(?!\d)"),    # 20260518
    re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일"),       # 5월 18일
    re.compile(r"(?<!\d)(\d{1,2})[/.](\d{1,2})(?!\d)"),   # 5/18, 5.18
]


def date_from_title(title: str, year_hint: int) -> date | None:
    """제목에서 방송일 표기를 추출한다. 연도가 생략된 경우 year_hint 사용."""
    if not title:
        return None
    for pat in TITLE_DATE_PATTERNS:
        m = pat.search(title)
        if not m:
            continue
        groups = m.groups()
        try:
            if len(groups) == 3:
                y, mo, d = int(groups[0]), int(groups[1]), int(groups[2])
            else:
                y = year_hint
                mo, d = int(groups[0]), int(groups[1])
            return date(y, mo, d)
        except ValueError:
            continue
    return None


def pubdate_from_entry(entry) -> date | None:
    """RSS pubDate의 원본 타임존을 보존해 날짜를 추출한다.

    feedparser의 published_parsed는 UTC로 정규화되어 있어 KST(+0900) 방송이
    하루 어긋날 수 있다. published 원본 문자열을 우선 파싱해 publisher의
    타임존 기준 날짜를 반환한다.
    """
    raw = entry.get("published") or entry.get("updated")
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
            return dt.date()
        except (TypeError, ValueError, IndexError):
            pass
    pp: struct_time | None = entry.get("published_parsed") or entry.get(
        "updated_parsed"
    )
    if pp is None:
        return None
    return date(pp.tm_year, pp.tm_mon, pp.tm_mday)


def entry_date(entry, prefer: str = "title") -> date | None:
    """방송일 추출. prefer='title'이면 제목 우선, 없으면 pubDate fallback."""
    pub = pubdate_from_entry(entry)
    if prefer == "pubdate":
        return pub
    title = entry.get("title", "")
    year_hint = pub.year if pub else date.today().year
    from_title = date_from_title(title, year_hint)
    if from_title is not None:
        return from_title
    return pub


def list_episodes(feed_url: str, prefer: str = "title") -> list[dict]:
    feed = feedparser.parse(feed_url)
    episodes = []
    for entry in feed.entries:
        audio_url = None
        for enc in entry.get("enclosures", []):
            if enc.get("type", "").startswith("audio"):
                audio_url = enc.get("href")
                break
        if not audio_url:
            continue
        episodes.append(
            {
                "title": entry.get("title", "(제목 없음)"),
                "published": entry.get("published", ""),
                "date": entry_date(entry, prefer=prefer),
                "pub_date": pubdate_from_entry(entry),
                "url": audio_url,
            }
        )
    return episodes


def parse_date(s: str) -> date:
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError as e:
        raise SystemExit(f"날짜 형식 오류 '{s}': YYYY-MM-DD 형식이어야 합니다.") from e


def filter_by_date(
    episodes: list[dict],
    target: date | None,
    start: date | None,
    end: date | None,
) -> list[dict]:
    out = []
    for ep in episodes:
        d = ep["date"]
        if d is None:
            continue
        if target is not None and d != target:
            continue
        if start is not None and d < start:
            continue
        if end is not None and d > end:
            continue
        out.append(ep)
    return out


def safe_name(s: str, limit: int = 80) -> str:
    return "".join(c if c.isalnum() or c in " -_." else "_" for c in s)[:limit]


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        with open(dest, "wb") as f, tqdm(
            total=total, unit="B", unit_scale=True, desc=dest.name
        ) as bar:
            for chunk in r.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    bar.update(len(chunk))


def make_dest(out_dir: Path, ep: dict) -> Path:
    d = ep["date"]
    prefix = d.strftime("%Y-%m-%d") if d else "nodate"
    return out_dir / f"{prefix}_{safe_name(ep['title'])}.mp3"


def main() -> int:
    p = argparse.ArgumentParser(description="Apple Podcasts 에피소드 다운로더")
    p.add_argument("apple_id", help="Apple Podcasts URL의 id 숫자 (예: 1494088127)")

    sel = p.add_argument_group("에피소드 선택 (하나만 사용)")
    sel.add_argument("--index", type=int, default=None, help="최신부터의 인덱스 (0=최신)")
    sel.add_argument("--date", dest="on_date", default=None, help="특정 방송 날짜 YYYY-MM-DD")
    sel.add_argument("--from", dest="from_date", default=None, help="시작 날짜 YYYY-MM-DD (포함)")
    sel.add_argument("--to", dest="to_date", default=None, help="종료 날짜 YYYY-MM-DD (포함)")

    p.add_argument(
        "--by",
        choices=["title", "pubdate"],
        default="title",
        help="날짜 판단 기준 (기본: title - 제목 내 방송일 우선)",
    )
    p.add_argument("--limit", type=int, default=30, help="목록 표시 시 최대 개수 (기본: 30)")
    p.add_argument("--out-dir", default="audio", help="다운로드 저장 폴더 (기본: audio)")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="다운로드 없이 매칭되는 에피소드만 출력",
    )

    tr = p.add_argument_group("전사 (다운로드 후 자동 실행)")
    tr.add_argument(
        "--transcribe",
        action="store_true",
        help="다운로드 직후 faster-whisper로 전사도 수행",
    )
    tr.add_argument("--model", default="large-v3-turbo", help="Whisper 모델 (기본: large-v3-turbo)")
    tr.add_argument("--language", default="ko", help="언어 코드 (기본: ko)")
    tr.add_argument("--prompt", default=None, help="초기 프롬프트 (고유명사 표기 유도)")
    tr.add_argument(
        "--formats",
        default="txt,srt,md",
        help="전사 출력 포맷 (기본: txt,srt,md). 선택: txt,srt,md,json",
    )
    tr.add_argument(
        "--device",
        choices=["auto", "cpu", "cuda"],
        default="auto",
        help="추론 디바이스 (기본: auto - GPU 있으면 cuda)",
    )
    tr.add_argument("--overwrite", action="store_true", help="기존 전사 결과 덮어쓰기")

    args = p.parse_args()

    on_date = parse_date(args.on_date) if args.on_date else None
    from_date = parse_date(args.from_date) if args.from_date else None
    to_date = parse_date(args.to_date) if args.to_date else None

    if on_date and (from_date or to_date):
        raise SystemExit("--date 와 --from/--to 는 함께 사용할 수 없습니다.")
    if from_date and to_date and from_date > to_date:
        raise SystemExit("--from 이 --to 보다 늦습니다.")

    feed_url = lookup_feed_url(args.apple_id)
    print(f"피드: {feed_url}")
    episodes = list_episodes(feed_url, prefer=args.by)
    print(f"피드에서 {len(episodes)}개 에피소드 발견 (날짜 기준: {args.by})\n")
    if not episodes:
        return 1

    date_filter_used = on_date or from_date or to_date
    downloaded: list[Path] = []

    if date_filter_used:
        targets = filter_by_date(episodes, on_date, from_date, to_date)
        if not targets:
            print("조건에 맞는 에피소드가 없습니다.")
            return 1
        print(f"매칭된 에피소드 {len(targets)}개:")
        for ep in targets:
            print(f"  {ep['date']}  {ep['title']}")
        if args.dry_run:
            return 0
        out_dir = Path(args.out_dir)
        for ep in targets:
            dest = make_dest(out_dir, ep)
            if dest.exists():
                print(f"\n건너뜀(이미 존재): {dest.name}")
            else:
                print(f"\n다운로드: {ep['title']}\n  -> {dest}")
                download(ep["url"], dest)
            downloaded.append(dest)
        print(f"\n다운로드 완료. 저장 폴더: {out_dir.resolve()}")
    elif args.index is None:
        for i, ep in enumerate(episodes[: args.limit]):
            print(f"[{i:>2}] {ep['date']}  {ep['title']}")
        if len(episodes) > args.limit:
            print(f"... ({len(episodes) - args.limit}개 더 있음, --limit 조정)")
        print("\n--index N, --date YYYY-MM-DD, --from/--to 옵션으로 다운로드하세요.")
        return 0
    else:
        if args.index < 0 or args.index >= len(episodes):
            raise SystemExit(f"--index 범위 오류: 0~{len(episodes) - 1}")
        ep = episodes[args.index]
        dest = make_dest(Path(args.out_dir), ep)
        if dest.exists():
            print(f"건너뜀(이미 존재): {dest.name}")
        else:
            print(f"다운로드: {ep['title']}\n  -> {dest}")
            download(ep["url"], dest)
        downloaded.append(dest)
        print(f"\n다운로드 완료: {dest.resolve()}")

    if args.transcribe and downloaded:
        formats = {f.strip().lower() for f in args.formats.split(",") if f.strip()}
        print(f"\n=== 전사 시작 ({len(downloaded)}개 파일) ===")
        from transcribe import transcribe_files

        transcribe_files(
            files=downloaded,
            model_name=args.model,
            language=args.language,
            initial_prompt=args.prompt,
            overwrite=args.overwrite,
            formats=formats,
            device=args.device,
        )
        print("\n전사 완료.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
