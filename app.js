// 공용 헬퍼 함수. index.html, play.html 두 곳에서 모두 사용.

async function fetchJson(url) {
  const res = await fetch(url, { cache: "no-cache" });
  if (!res.ok) throw new Error(`HTTP ${res.status} for ${url}`);
  return res.json();
}

function pad2(n) {
  return n < 10 ? "0" + n : "" + n;
}

// 초 단위 -> HH:MM:SS (1시간 미만이면 MM:SS)
function formatDuration(sec) {
  if (sec == null) return "—";
  sec = Math.floor(sec);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return `${h}:${pad2(m)}:${pad2(s)}`;
  return `${m}:${pad2(s)}`;
}

// 자막 segment 시각 (MM:SS)
function formatTime(sec) {
  if (sec == null) return "—";
  sec = Math.floor(sec);
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${pad2(s)}`;
}

// XSS 방지용 HTML escape
function escape(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// 영문과 한글을 분리하여 한글 부분에 스타일(해석 표시) 적용
function formatText(s) {
  if (!s) return "";
  let esc = escape(s);
  // 한글이 포함된 문장이나 어구를 span으로 감싸기
  // 간단한 휴리스틱: 한글 유니코드 범위 묶음
  return esc.replace(/([가-힣ㄱ-ㅎㅏ-ㅣ]+[가-힣ㄱ-ㅎㅏ-ㅣ\s\d.,!?~]*)/g, '<span class="kor-text">$1</span>');
}

// iTunes Search API로 앨범 아트 조회
const _artworkCache = {};
async function fetchArtwork(title, size) {
  size = size || 600;
  if (!title) return null;
  // "Screen English - xxx" 같은 제목은 영화 대사라 앨범아트 없음
  if (/screen\s*english/i.test(title)) return null;
  // 제목에서 곡명 - 아티스트 추출 (파일명의 _ 를 ' 로 복원)
  let query = title.replace(/_/g, "'").trim();
  // "(06/01/월)" 같은 날짜 접두사 제거
  query = query.replace(/^\(\d{2}\/\d{2}\/[월화수목금토일]\)\s*/, "");
  // 앞뒤 공백/특수문자 정리
  query = query.replace(/[^\w\s\-'가-힣]/g, " ").trim();
  if (!query || query.length < 3) return null;

  if (_artworkCache[query]) return _artworkCache[query];

  try {
    const url = `https://itunes.apple.com/search?term=${encodeURIComponent(query)}&media=music&limit=1`;
    const res = await fetch(url);
    if (!res.ok) return null;
    const data = await res.json();
    if (data.results && data.results.length > 0) {
      // 100x100 → 원하는 크기로 변환
      const artUrl = data.results[0].artworkUrl100
        .replace("100x100", `${size}x${size}`);
      _artworkCache[query] = artUrl;
      return artUrl;
    }
  } catch (e) {
    console.warn("artwork fetch failed:", e);
  }
  _artworkCache[query] = null;
  return null;
}

// 재생 속도 변경
function changeSpeed(delta) {
  const audio = document.getElementById("audio");
  if (!audio) return;
  let rate = Math.round((audio.playbackRate + delta) * 10) / 10;
  rate = Math.max(0.5, Math.min(2.0, rate));
  audio.playbackRate = rate;
  const disp = document.getElementById("speed-display");
  if (disp) disp.textContent = rate.toFixed(1) + "x";
}

// 구간 반복 토글
let repeatMode = "off"; // "off" | "segment" | "full"
function toggleRepeat() {
  const btn = document.getElementById("repeat-btn");
  if (!btn) return;
  if (repeatMode === "off") {
    repeatMode = "segment";
    btn.classList.add("active");
    btn.textContent = "🔁 구간";
  } else if (repeatMode === "segment") {
    repeatMode = "full";
    btn.classList.remove("active");
    btn.style.background = "#a855f7";
    btn.style.color = "#fff";
    btn.textContent = "🔁 전체";
  } else {
    repeatMode = "off";
    btn.classList.remove("active");
    btn.style.background = "";
    btn.style.color = "";
    btn.textContent = "🔁 반복";
  }
}

// 연속 재생 토글
let autoNext = true;
function toggleAutoNext() {
  const btn = document.getElementById("auto-next-btn");
  if (!btn) return;
  autoNext = !autoNext;
  btn.classList.toggle("active", autoNext);
}
