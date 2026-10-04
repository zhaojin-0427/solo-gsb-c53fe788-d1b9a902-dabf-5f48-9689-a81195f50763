"use strict";

const $ = (id) => document.getElementById(id);

function fmt(iso) { return iso ? iso.replace("T", " ").slice(0, 16) : ""; }

async function refreshStatus() {
  const r = await fetch("/api/status");
  const s = await r.json();
  const info = $("datasetInfo");
  if (!s.imported) {
    info.textContent = "当前无可用数据集。";
    return;
  }
  const st = s.stats;
  info.innerHTML = `当前数据集 #${s.dataset_id}（${s.imported_at}，时区 ${s.timezone}）：`
    + `${st.agency} agency、${st.routes} 线路、${st.trips} 班次、`
    + `${st.stop_times} 条站时、${st.transfers} 条换乘边。`;
}

async function loadStops(q) {
  const r = await fetch(`/api/stops?q=${encodeURIComponent(q || "")}`);
  const d = await r.json();
  $("stopList").innerHTML = "";
  for (const s of d.stops) {
    const o = document.createElement("option");
    o.value = `${s.stop_name} (${s.stop_id})`;
    $("stopList").appendChild(o);
  }
}

// "站名 (S1)" → "S1"；已经是裸 ID 则原样返回
function resolveStopId(v) {
  const m = /\(([^()]+)\)\s*$/.exec(v.trim());
  return m ? m[1] : v.trim();
}

let stopTimer = null;
function onStopInput(el) {
  clearTimeout(stopTimer);
  stopTimer = setTimeout(() => loadStops(el.value), 200);
}
$("origin").addEventListener("input", (e) => onStopInput(e.target));
$("destination").addEventListener("input", (e) => onStopInput(e.target));
$("origin").addEventListener("focus", () => loadStops($("origin").value));
$("destination").addEventListener("focus", () => loadStops($("destination").value));

$("btnImport").addEventListener("click", async () => {
  const f = $("gtfsFile").files[0];
  const msg = $("importMsg");
  if (!f) { msg.className = "msg err"; msg.textContent = "请先选择 ZIP 文件。"; return; }
  $("btnImport").disabled = true;
  msg.className = "meta"; msg.textContent = "上传与校验中……（10 万条站时约需数秒）";
  const fd = new FormData();
  fd.append("file", f);
  try {
    const r = await fetch("/api/import", { method: "POST", body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "导入失败");
    msg.className = "msg ok";
    msg.textContent = `导入成功：${d.stops} 站、${d.routes} 线路、${d.trips} 班次、`
      + `${d.stop_times} 条站时、${d.transfers} 换乘边。`;
    await refreshStatus();
  } catch (e) {
    msg.className = "msg err";
    msg.textContent = e.message;
  } finally {
    $("btnImport").disabled = false;
  }
});

function renderJourney(j) {
  let rows = "";
  j.segments.forEach((s, i) => {
    rows += `<tr class="seg">
      <td>${i + 1}</td>
      <td><span class="badge">${s.route_name || s.trip_id}</span>
          ${s.headsign ? "→ " + escapeHtml(s.headsign) : ""}<br>
          <small>班次 ${escapeHtml(s.trip_id)}</small></td>
      <td>${escapeHtml(s.from_stop_name || s.from_stop_id)}
          <small>(${escapeHtml(s.from_stop_id)})</small></td>
      <td>${escapeHtml(s.to_stop_name || s.to_stop_id)}
          <small>(${escapeHtml(s.to_stop_id)})</small></td>
      <td>${fmt(s.board_time)}</td>
      <td>${fmt(s.alight_time)}</td></tr>`;
  });
  return `<div class="journey">
    <div class="hd">
      <span>🚏 出发 <b>${fmt(j.departure_time)}</b> ｜ 🏁 到达 <b>${fmt(j.arrival_time)}</b></span>
      <span>换乘 <b>${j.transfers}</b> 次 ｜ ${j.segments.length} 个乘车段</span>
    </div>
    <table><thead><tr><th>#</th><th>线路/班次</th><th>上车站</th><th>下车站</th>
      <th>上车时刻</th><th>下车时刻</th></tr></thead><tbody>${rows}</tbody></table>
  </div>`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

$("btnPlan").addEventListener("click", async () => {
  const msg = $("planMsg"), box = $("results");
  const params = new URLSearchParams({
    origin: resolveStopId($("origin").value),
    destination: resolveStopId($("destination").value),
    date: $("date").value,
    time: $("time").value,
    max_transfers: $("maxTransfers").value,
  });
  msg.className = "msg"; msg.textContent = "";
  box.innerHTML = `<div class="empty">搜索中……</div>`;
  const t0 = performance.now();
  try {
    const r = await fetch(`/api/plan?${params}`);
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "查询失败");
    const ms = Math.round(performance.now() - t0);
    if (d.journeys.length === 0) {
      box.innerHTML = `<div class="empty">无解：在出发后 24 小时窗口内没有可达行程（返回空列表）。耗时 ${ms} ms。</div>`;
    } else {
      box.innerHTML = d.journeys.map(renderJourney).join("")
        + `<div class="meta" style="margin-top:8px">共 ${d.journeys.length} 条非支配行程，耗时 ${ms} ms。</div>`;
    }
  } catch (e) {
    box.innerHTML = "";
    msg.className = "msg err";
    msg.textContent = e.message;
  }
});

(async function init() {
  const today = new Date();
  $("date").value = today.toISOString().slice(0, 10);
  await refreshStatus();
  await loadStops("");
})();
