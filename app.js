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
