"""N-gram 기반 코너 경계(시작/종료) 유사도 감지기.

레이블 예제로부터 character n-gram 프로파일을 생성하고,
새로운 세그먼트 텍스트와의 Jaccard 유사도를 계산하여
시작/종료 지점을 감지한다.

사용:
  import ngram_detector as nd
  detector = nd.NgramDetector.from_json("references/ngram_labels.json")
  start_seg, score = detector.find_start(segments, min_t=90, max_t=720)
  end_seg, score   = detector.find_end(segments, min_t=600, max_t=1320)
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except AttributeError:
    pass


@dataclass
class Segment:
    """extract_corner.Segment와 호환되는 최소 구조체."""
    index: int
    start: float
    end: float
    text: str


# ──────────────────────────────────────────────
# N-gram 유틸리티
# ──────────────────────────────────────────────

def _normalize(text: str) -> str:
    """대소문자 통일 + 연속 공백 축소."""
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def char_ngrams(text: str, n: int = 3) -> set[str]:
    """문자 단위 n-gram 집합을 생성한다."""
    text = _normalize(text)
    if len(text) < n:
        return {text}
    return {text[i : i + n] for i in range(len(text) - n + 1)}


def word_ngrams(text: str, n: int = 2) -> set[str]:
    """단어 단위 n-gram 집합을 생성한다."""
    words = _normalize(text).split()
    if len(words) < n:
        return {" ".join(words)}
    return {" ".join(words[i : i + n]) for i in range(len(words) - n + 1)}


def combined_ngrams(text: str) -> set[str]:
    """character 3-gram + word 2-gram을 합친 프로파일."""
    return char_ngrams(text, 3) | word_ngrams(text, 2)


def jaccard(a: set, b: set) -> float:
    """두 집합의 Jaccard 유사도."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


# ──────────────────────────────────────────────
# N-gram 감지기 클래스
# ──────────────────────────────────────────────

class NgramDetector:
    """레이블 예제 기반 n-gram 유사도 코너 경계 감지기."""

    def __init__(
        self,
        start_profiles: list[set[str]],
        end_profiles: list[set[str]],
        start_threshold: float = 0.10,
        end_threshold: float = 0.10,
        window: int = 4,
    ):
        self.start_profiles = start_profiles
        self.end_profiles = end_profiles
        self.start_threshold = start_threshold
        self.end_threshold = end_threshold
        self.window = window

    @classmethod
    def from_json(cls, path: str | Path, **kwargs) -> "NgramDetector":
        """JSON 레이블 파일로부터 감지기를 생성한다."""
        path = Path(path)
        if not path.exists():
            return cls([], [], **kwargs)
        data = json.loads(path.read_text(encoding="utf-8"))
        start_profiles = [combined_ngrams(ex) for ex in data.get("start_examples", [])]
        end_profiles = [combined_ngrams(ex) for ex in data.get("end_examples", [])]
        return cls(start_profiles, end_profiles, **kwargs)

    def _score_against_profiles(
        self, text: str, profiles: list[set[str]]
    ) -> float:
        """텍스트와 모든 프로파일 간 최대 Jaccard 유사도를 반환."""
        if not profiles:
            return 0.0
        text_ngrams = combined_ngrams(text)
        return max(jaccard(text_ngrams, p) for p in profiles)

    def _build_windows(
        self,
        segments: list,
        min_seconds: float,
        max_seconds: float,
    ) -> list[tuple[int, str, float]]:
        """시간 범위 내 세그먼트를 sliding window로 묶어 (index, combined_text, start_time) 리스트를 반환."""
        results = []
        for i, seg in enumerate(segments):
            t = seg.start if hasattr(seg, "start") else seg["start"]
            if t < min_seconds:
                continue
            if t > max_seconds:
                break
            end_i = min(i + self.window, len(segments))
            combined = " ".join(
                (s.text if hasattr(s, "text") else s["text"])
                for s in segments[i:end_i]
            )
            results.append((i, combined, t))
        return results

    def find_start(
        self,
        segments: list,
        min_seconds: float = 90.0,
        max_seconds: float = 720.0,
    ) -> tuple | None:
        """시작 지점 후보를 찾아 (segment, score) 를 반환. 미감지 시 None.

        반환되는 segment는 입력 segments 리스트 내의 원본 객체.
        """
        if not self.start_profiles:
            return None

        windows = self._build_windows(segments, min_seconds, max_seconds)
        if not windows:
            return None

        best_idx = -1
        best_score = 0.0
        for idx, text, _ in windows:
            score = self._score_against_profiles(text, self.start_profiles)
            if score > best_score:
                best_score = score
                best_idx = idx

        if best_score < self.start_threshold or best_idx < 0:
            return None

        return segments[best_idx], best_score

    def find_end(
        self,
        segments: list,
        min_seconds: float = 600.0,
        max_seconds: float = 1320.0,
    ) -> tuple | None:
        """종료 지점 후보를 찾아 (segment, score) 를 반환. 미감지 시 None.

        반환되는 segment는 입력 segments 리스트 내의 원본 객체.
        """
        if not self.end_profiles:
            return None

        windows = self._build_windows(segments, min_seconds, max_seconds)
        if not windows:
            return None

        best_idx = -1
        best_score = 0.0
        for idx, text, _ in windows:
            score = self._score_against_profiles(text, self.end_profiles)
            if score > best_score:
                best_score = score
                best_idx = idx

        if best_score < self.end_threshold or best_idx < 0:
            return None

        # 윈도우 내에서 마지막 관련 세그먼트를 종료점으로 잡기 (기존 로직과 동일)
        end_i = min(best_idx + self.window, len(segments))
        closing_re = re.compile(
            r"여기까지|마무리|see\s*you|bye|만나|볼게요", re.IGNORECASE
        )
        last_match = None
        for j in range(best_idx, end_i):
            seg_text = segments[j].text if hasattr(segments[j], "text") else segments[j]["text"]
            if closing_re.search(seg_text):
                last_match = segments[j]

        return (last_match or segments[best_idx]), best_score


# ──────────────────────────────────────────────
# CLI 테스트 인터페이스
# ──────────────────────────────────────────────

def main() -> None:
    """CLI: python ngram_detector.py <srt_file> [--labels <json>]"""
    import argparse

    p = argparse.ArgumentParser(description="N-gram 코너 경계 감지 테스트")
    p.add_argument("srt", help="SRT 파일 경로")
    p.add_argument(
        "--labels",
        default="references/ngram_labels.json",
        help="레이블 JSON 경로 (기본: references/ngram_labels.json)",
    )
    args = p.parse_args()

    # extract_corner의 parse_srt 재사용
    try:
        import extract_corner as ec
        segments = ec.parse_srt(Path(args.srt))
    except ImportError:
        print("extract_corner를 import 할 수 없습니다.", file=sys.stderr)
        return

    detector = NgramDetector.from_json(args.labels)
    print(f"레이블: start {len(detector.start_profiles)}개, end {len(detector.end_profiles)}개\n")

    result_start = detector.find_start(segments)
    if result_start:
        seg, score = result_start
        minutes = int(seg.start // 60)
        seconds = int(seg.start % 60)
        print(f"START: seg#{seg.index} [{minutes:02d}:{seconds:02d}] (score={score:.3f})")
        print(f"  text: {seg.text[:100]}")
    else:
        print("START: 감지 실패")

    result_end = detector.find_end(segments)
    if result_end:
        seg, score = result_end
        minutes = int(seg.start // 60)
        seconds = int(seg.start % 60)
        print(f"END:   seg#{seg.index} [{minutes:02d}:{seconds:02d}] (score={score:.3f})")
        print(f"  text: {seg.text[:100]}")
    else:
        print("END:   감지 실패")


if __name__ == "__main__":
    main()
