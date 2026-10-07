// Deploy helper: run a shell command on the server through the Ake Server
// panel's terminal websocket (use when SSH/Cloudflare Access is unavailable).
//
//   node deploy_via_panel.mjs <csrf> <cookie> '<command>'
//
// Get <csrf> and <cookie> by logging in first:
//   curl -sc jar -H "Origin: https://server.policeshield4.com" #        -H "Referer: https://server.policeshield4.com/" -H "Content-Type: application/json" #        --data-raw '{"username":"<user>","password":"<pw>"}' #        https://server.policeshield4.com/api/login     -> {"csrf": "..."}
//   COOKIE=$(grep -i session jar | awk '{print $6"="$7}' | paste -sd";")
// See docs/DEPLOYMENT_2026-10-07_9f0b601_rag.md for the full walkthrough.
const CSRF = process.argv[2], COOKIE = process.argv[3], COMMANDS = process.argv[4];
const ws = new WebSocket("wss://server.policeshield4.com/api/terminal?csrf=" + encodeURIComponent(CSRF),
  { headers: { Cookie: COOKIE, "User-Agent": "Mozilla/5.0", Origin: "https://server.policeshield4.com" } });
let out = "";
ws.onmessage = (e) => {
  try {
    const msg = JSON.parse(e.data);
    if (msg.type === "output") out += msg.data;
    if (msg.type === "ready") {
      setTimeout(() => ws.send(JSON.stringify({ type: "input", data: "\r" })), 400);
      setTimeout(() => ws.send(JSON.stringify({ type: "input", data: COMMANDS + " ; echo D0NE-RC=$?\r" })), 1600);
    }
  } catch {}
};
const timer = setInterval(() => {
  if (/D0NE-RC=\d/.test(out)) { clearInterval(timer); finish(0); }
}, 500);
const kill = setTimeout(() => { clearInterval(timer); finish(1); }, 150000);
function finish(code) {
  clearTimeout(kill);
  let clean = out.replace(/\u001b\][^\u001b]*/g, " ");
  clean = clean.replace(/\u001b\[[0-9;?]*[a-zA-Z]/g, "");
  const lines = clean.split("\r\n").join("\n").split("\n")
    .map(l => l.trim()).filter(Boolean);
  console.log(lines.join("\n"));
  try { ws.close(); } catch {}
  process.exit(code);
}
