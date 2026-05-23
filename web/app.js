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
