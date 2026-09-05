"""Canlı izleme paneli — http://localhost:8484

    python -m src.panel

Bot sürecinden bağımsız çalışır: SQLite durumunu (WAL eşzamanlı okuma) ve
Binance canlı fiyatlarını okur; emir GÖNDERMEZ, yalnızca izler. 5 saniyede bir
kendini yeniler: açık pozisyonların anlık kâr/zararı, varlık eğrisi, işlem
geçmişi ve günlük performans.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import CONFIG
from .exchange import MarketData
from .state import StateStore

log = logging.getLogger("panel")
market = MarketData()
state = StateStore(CONFIG.db_path)


def build_state() -> dict:
    balance = float(state.get_kv("fut_usdt", "10000.0"))
    positions = []
    equity = balance
    for p in state.all_positions():
        try:
            price = market.last_price(p.symbol)
        except Exception:
            price = p.entry_price
        upnl = (p.qty * (price - p.entry_price) if p.side == "LONG"
                else p.qty * (p.entry_price - price)) - p.funding_acc
        pos_notional = p.qty * p.entry_price
        equity += p.margin + upnl
        positions.append({
            "symbol": p.symbol, "side": p.side, "qty": p.qty,
            "entry": p.entry_price, "price": price, "stop": p.trailing_stop,
            "margin": round(p.margin, 2), "funding": round(p.funding_acc, 2),
            "upnl": round(upnl, 2),
            "upnl_pct": round(upnl / p.margin * 100, 2) if p.margin else 0,
            "notional": round(pos_notional, 2),
            "since": p.entry_time[:16].replace("T", " "),
        })

    stats = state.trade_stats()
    win_rate = stats["wins"] / stats["count"] * 100 if stats["count"] else 0
    return {
        "now": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
        "mode": CONFIG.mode, "leverage": CONFIG.leverage,
        "allow_short": CONFIG.allow_short,
        "symbols": list(CONFIG.symbols),
        "balance": round(balance, 2), "equity": round(equity, 2),
        "total_pnl": round(equity - 10000, 2),
        "total_pnl_pct": round((equity / 10000 - 1) * 100, 2),
        "day_realized": round(state.todays_realized_pnl(), 2),
        "n_trades": stats["count"], "win_rate": round(win_rate, 1),
        "cum_realized": round(stats["total_pnl"], 2),
        "positions": positions,
        "trades": state.recent_trades(30),
        "equity_history": [{"t": t, "v": round(v, 2)} for t, v in state.equity_history(600)],
        "assessments": state.latest_assessments(),
    }


PAGE = """<!doctype html>
<html lang="tr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AL-SAT Paneli</title>
<style>
  :root {
    --bg:#0c0e12; --card:#14171d; --card2:#191d24; --line:#242a33;
    --ink:#e8eaed; --ink2:#9aa3ad; --mut:#5c6570;
    --up:#2fbf71; --down:#e5544b; --accent:#4d9fec; --amber:#e0a63c;
  }
  @media (prefers-color-scheme: light) {
    :root { --bg:#f4f5f7; --card:#ffffff; --card2:#f0f1f4; --line:#e2e4e9;
      --ink:#15181d; --ink2:#555c66; --mut:#8a919b;
      --up:#188952; --down:#c73e36; --accent:#2472c8; --amber:#a8731a; }
  }
  * { box-sizing:border-box; margin:0; }
  body { background:var(--bg); color:var(--ink);
    font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; padding:22px; }
  .top { display:flex; align-items:baseline; gap:14px; flex-wrap:wrap; margin-bottom:18px; }
  h1 { font-size:18px; font-weight:650; }
  .chip { font-size:11.5px; padding:3px 10px; border-radius:99px; background:var(--card2);
    color:var(--ink2); border:1px solid var(--line); }
  .chip.warn { color:var(--amber); border-color:var(--amber); }
  #clock { margin-left:auto; color:var(--mut); font-variant-numeric:tabular-nums; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:10px; margin-bottom:14px; }
  .stat { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 15px; }
  .stat .l { font-size:11px; color:var(--mut); text-transform:uppercase; letter-spacing:.05em; }
  .stat .v { font-size:22px; font-weight:650; margin-top:2px; font-variant-numeric:tabular-nums; }
  .stat .s { font-size:11.5px; color:var(--ink2); font-variant-numeric:tabular-nums; }
  .up { color:var(--up); } .down { color:var(--down); }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
    padding:14px 16px; margin-bottom:14px; }
  .card h2 { font-size:13px; color:var(--ink2); text-transform:uppercase;
    letter-spacing:.05em; font-weight:600; margin-bottom:10px; }
  table { width:100%; border-collapse:collapse; font-size:13px; font-variant-numeric:tabular-nums; }
  th { text-align:right; color:var(--mut); font-weight:500; font-size:11px;
    text-transform:uppercase; padding:4px 8px; border-bottom:1px solid var(--line); }
  td { text-align:right; padding:7px 8px; border-bottom:1px solid var(--line); }
  th:first-child, td:first-child { text-align:left; }
  tr:last-child td { border-bottom:none; }
  .side { font-size:11px; font-weight:700; padding:2px 7px; border-radius:5px; }
  .side.L { background:color-mix(in srgb, var(--up) 18%, transparent); color:var(--up); }
  .side.S { background:color-mix(in srgb, var(--down) 18%, transparent); color:var(--down); }
  .empty { color:var(--mut); padding:14px 0; text-align:center; }
  svg { display:block; width:100%; }
  .pulse { animation:pulse 2s infinite; display:inline-block; width:7px; height:7px;
    border-radius:50%; background:var(--up); margin-right:6px; vertical-align:1px; }
  @keyframes pulse { 50% { opacity:.35; } }
  @media (prefers-reduced-motion: reduce) { .pulse { animation:none; } }
</style></head><body>
<div class="top">
  <h1><span class="pulse"></span>AL-SAT Paneli</h1>
  <span class="chip" id="mode"></span>
  <span class="chip" id="lev"></span>
  <span class="chip warn" id="paper">PAPER — gerçek para riski yok</span>
  <span id="clock"></span>
</div>
<div class="grid" id="stats"></div>
<div class="card"><h2>Ön Değerlendirme — her mum kapanışında 5 araçlı analiz (incelemesiz giriş yok)</h2><div id="assess"></div></div>
<div class="card"><h2>Açık Pozisyonlar — anlık kâr/zarar</h2><div id="positions"></div></div>
<div class="card"><h2>Varlık Eğrisi</h2><div id="chart"><div class="empty">Veri birikiyor…</div></div></div>
<div class="card"><h2>Son İşlemler</h2><div id="trades"></div></div>
<script>
const $ = id => document.getElementById(id);
const money = v => (v<0?"−$":"$") + Math.abs(v).toLocaleString("tr-TR",{minimumFractionDigits:2,maximumFractionDigits:2});
const cls = v => v > 0 ? "up" : v < 0 ? "down" : "";
const sign = v => (v>0?"+":"") + v.toLocaleString("tr-TR",{maximumFractionDigits:2});

async function refresh() {
  let d;
  try { d = await (await fetch("/api/state")).json(); }
  catch { $("clock").textContent = "bağlantı koptu — yeniden denenecek"; return; }

  $("clock").textContent = d.now;
  $("mode").textContent = "mod: " + d.mode;
  $("lev").textContent = d.leverage + "x kaldıraç" + (d.allow_short ? " · long+short" : " · sadece long");
  $("paper").style.display = d.mode.includes("paper") || d.mode === "dry_run" ? "" : "none";

  $("stats").innerHTML = [
    ["Toplam Varlık", money(d.equity), sign(d.total_pnl_pct) + "% başlangıçtan", d.total_pnl],
    ["Serbest Bakiye", money(d.balance), "marjin dışı", 0],
    ["Bugün Gerçekleşen", money(d.day_realized), "kapanan işlemlerden", d.day_realized],
    ["Toplam İşlem", d.n_trades, "kazanma %" + d.win_rate, 0],
    ["Kümülatif PnL", money(d.cum_realized), "kapanmış işlemler", d.cum_realized],
  ].map(([l,v,s,c]) =>
    `<div class="stat"><div class="l">${l}</div><div class="v ${cls(c)}">${v}</div><div class="s">${s}</div></div>`
  ).join("");

  $("assess").innerHTML = d.assessments.length ? "<table><tr>" +
    "<th>Parite</th><th>Karar</th><th>Skor</th><th style='text-align:left'>Araç Oyları</th><th>Zaman</th></tr>" +
    d.assessments.map(a => {
      const col = a.decision === "LONG-UYGUN" ? "up" : a.decision === "SHORT-UYGUN" ? "down" : "";
      const votes = a.veto ? `<span class="down">VETO: ${a.veto}</span>`
        : (a.votes||[]).map(v => `${v.tool} <b class="${cls(v.score)}">${v.score>0?"+":""}${v.score}</b>`).join(" · ");
      return `<tr><td><b>${a.symbol}</b></td>
        <td class="${col}"><b>${a.decision}</b></td>
        <td class="${cls(a.score)}">${a.score>0?"+":""}${a.score}</td>
        <td style="text-align:left;color:var(--ink2);font-size:12px">${votes}</td>
        <td style="color:var(--mut)">${(a.ts||"").slice(11,16)} UTC</td></tr>`;
    }).join("") + "</table>"
    : `<div class="empty">İlk mum kapanışı bekleniyor — analiz her 4 saatte bir yenilenir</div>`;

  $("positions").innerHTML = d.positions.length ? "<table><tr>" +
    "<th>Parite</th><th>Yön</th><th>Giriş</th><th>Anlık</th><th>Stop</th><th>Marjin</th><th>Funding</th><th>Anlık PnL</th><th>%</th></tr>" +
    d.positions.map(p => `<tr>
      <td><b>${p.symbol}</b> <span style="color:var(--mut);font-size:11px">${p.since}</span></td>
      <td><span class="side ${p.side[0]}">${p.side}</span></td>
      <td>$${p.entry.toLocaleString("tr-TR")}</td>
      <td>$${p.price.toLocaleString("tr-TR")}</td>
      <td>$${p.stop.toLocaleString("tr-TR",{maximumFractionDigits:2})}</td>
      <td>${money(p.margin)}</td><td>${money(p.funding)}</td>
      <td class="${cls(p.upnl)}"><b>${money(p.upnl)}</b></td>
      <td class="${cls(p.upnl)}">${sign(p.upnl_pct)}%</td></tr>`).join("") + "</table>"
    : `<div class="empty">Açık pozisyon yok — bot sinyal bekliyor (${d.symbols.join(", ")})</div>`;

  if (d.equity_history.length > 1) {
    const H = 160, W = 900, pts = d.equity_history;
    const vs = pts.map(p => p.v), mn = Math.min(...vs, 9990), mx = Math.max(...vs, 10010);
    const X = i => 40 + i / (pts.length - 1) * (W - 50);
    const Y = v => 8 + (mx - v) / (mx - mn) * (H - 30);
    const path = pts.map((p,i) => (i?"L":"M") + X(i).toFixed(1) + " " + Y(p.v).toFixed(1)).join("");
    const base = Y(10000);
    $("chart").innerHTML = `<svg viewBox="0 0 ${W} ${H}">
      <line x1="40" x2="${W-10}" y1="${base}" y2="${base}" stroke="var(--line)" stroke-dasharray="4 4"/>
      <text x="36" y="${base+4}" text-anchor="end" font-size="10" fill="var(--mut)">10k</text>
      <path d="${path}" fill="none" stroke="var(--accent)" stroke-width="2"/>
      <circle cx="${X(pts.length-1)}" cy="${Y(vs.at(-1))}" r="3.5" fill="var(--accent)"/>
      <text x="${X(pts.length-1)-8}" y="${Y(vs.at(-1))-8}" text-anchor="end" font-size="11"
        fill="var(--ink)" font-weight="600">$${vs.at(-1).toLocaleString("tr-TR")}</text></svg>`;
  }

  $("trades").innerHTML = d.trades.length ? "<table><tr>" +
    "<th>Parite</th><th>Yön</th><th>Giriş → Çıkış</th><th>Net PnL</th><th>%</th><th>Neden</th><th>Kapanış</th></tr>" +
    d.trades.map(t => `<tr>
      <td><b>${t.symbol}</b></td>
      <td><span class="side ${(t.side||"LONG")[0]}">${t.side||"LONG"}</span></td>
      <td>$${(+t.entry_price).toLocaleString("tr-TR")} → $${(+t.exit_price).toLocaleString("tr-TR")}</td>
      <td class="${cls(t.pnl_usdt)}"><b>${money(t.pnl_usdt)}</b></td>
      <td class="${cls(t.pnl_usdt)}">${sign(t.pnl_pct)}%</td>
      <td style="color:var(--ink2)">${t.exit_reason}</td>
      <td style="color:var(--mut)">${(t.exit_time||"").slice(0,16).replace("T"," ")}</td></tr>`).join("") + "</table>"
    : `<div class="empty">Henüz kapanan işlem yok</div>`;
}
refresh();
setInterval(refresh, 5000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/state":
            try:
                body = json.dumps(build_state()).encode("utf-8")
                self._send(200, "application/json", body)
            except Exception as e:
                log.error("state hatası: %s", e)
                self._send(500, "application/json", b'{"error":"state"}')
        elif self.path == "/":
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        else:
            self._send(404, "text/plain", b"404")

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):  # erişim loglarını sustur
        pass


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    addr = ("127.0.0.1", CONFIG.panel_port)
    server = ThreadingHTTPServer(addr, Handler)
    print(f"Panel hazır: http://localhost:{CONFIG.panel_port}  (Ctrl+C ile durdurun)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
