"""状態表示（console / web / none）。

- console: interval_s ごとに系統別の最終受信時刻/レート/stale、マージ後 DMX の先頭8ch、
  フェイルオーバー履歴を 1 行ずつ端末へ出力する（curses 不要）。
- web: http.server で最小の状態 JSON(/status.json) と HTML(/) を web_port で提供する。
- none: 何も表示しない。

いずれも Receiver.snapshot() / recent_events() を参照するだけで、受信/送信ロジックには
触れない（読み取り専用）。クラッシュしても受信/送信は継続できるよう例外は握りつぶす。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


def make_status_reporter(receiver, cfg) -> "StatusReporter":
    """設定の status.mode に応じた StatusReporter を生成する。"""
    mode = (cfg.receiver.status.mode or "none").lower()
    if mode == "console":
        return ConsoleStatus(receiver, cfg)
    if mode == "web":
        return WebStatus(receiver, cfg)
    return StatusReporter(receiver, cfg)  # none = 何もしない基底


# --- 基底（none） -----------------------------------------------------------

class StatusReporter:
    """状態表示の基底。none モードはこのまま（何もしない）。"""

    def __init__(self, receiver, cfg) -> None:
        self.receiver = receiver
        self.cfg = cfg
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:  # none は起動しない
        logger.debug("状態表示: none（表示なし）")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)


# --- console ----------------------------------------------------------------

class ConsoleStatus(StatusReporter):
    """interval_s ごとに 1 行で状態を端末出力する。"""

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="status-console", daemon=True
        )
        self._thread.start()
        logger.debug("状態表示: console interval=%.2fs", self.cfg.receiver.status.interval_s)

    def _loop(self) -> None:
        interval = max(0.05, self.cfg.receiver.status.interval_s)
        # 最新イベントのタイムスタンプで新規判定する。件数比較だと events が
        # 最大10件で頭打ちになり、10件を超えると新規 stale/復帰が表示されなくなる。
        last_newest_t: Optional[float] = None
        while not self._stop.is_set():
            try:
                snap = self.receiver.snapshot()
                line = self._format(snap)
                # print は端末向け。ログとは別系統で見やすさを優先。
                print(line, flush=True)

                # 新規フェイルオーバーイベントがあれば併せて表示（events は新しい順）。
                events = snap.get("events", [])
                if events and events[0]["t"] != last_newest_t:
                    last_newest_t = events[0]["t"]
                    for ev in events[:3]:
                        kind = "STALE" if ev["kind"] == "stale" else "RECOVERED"
                        print(
                            f"  [failover] 系統{ev['source']}: {kind} @ t={ev['t']:.3f}",
                            flush=True,
                        )
            except Exception as e:  # noqa: BLE001  表示失敗で停止させない
                logger.debug("console 状態表示で例外を無視: %s", e)
            self._stop.wait(interval)

    @staticmethod
    def _format(snap: dict) -> str:
        parts = []
        for s in snap["sources"]:
            if not s["present"]:
                state = "----"
            elif s["stale"]:
                state = "STALE"
            else:
                state = "ok"
            age = "-" if s["age_s"] is None else f"{s['age_s']:.2f}s"
            parts.append(
                f"{s['name']}[{state} {s['rate_hz']:.0f}Hz age={age} f={s['frames']}]"
            )
        head = " ".join(f"{v:3d}" for v in snap["dmx_head"])
        st = snap["stats"]
        ts = time.strftime("%H:%M:%S")
        return (
            f"{ts} | " + "  ".join(parts)
            + f" | DMX1-8=[{head}]"
            + f" | tx={st['tx_frames']} rx={st['rx_artdmx']}/{st['rx_packets']}"
        )


# --- web --------------------------------------------------------------------

class WebStatus(StatusReporter):
    """http.server で状態 JSON / HTML を提供する。"""

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._serve, name="status-web", daemon=True
        )
        self._thread.start()

    def _serve(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        receiver = self.receiver
        port = self.cfg.receiver.status.web_port

        class Handler(BaseHTTPRequestHandler):
            # アクセスログを標準ログに流さない（端末を汚さない）。
            def log_message(self, fmt, *args):  # noqa: A002,N802
                logger.debug("web status: " + fmt, *args)

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except Exception:  # noqa: BLE001  クライアント切断で落とさない
                    pass

            def do_GET(self):  # noqa: N802
                try:
                    snap = receiver.snapshot()
                except Exception as e:  # noqa: BLE001
                    self._send(500, str(e).encode("utf-8"), "text/plain; charset=utf-8")
                    return
                if self.path.rstrip("/") in ("", "/status.json", "/status"):
                    if self.path.rstrip("/") == "":
                        body = _render_html(snap).encode("utf-8")
                        self._send(200, body, "text/html; charset=utf-8")
                    else:
                        body = json.dumps(snap, ensure_ascii=False).encode("utf-8")
                        self._send(200, body, "application/json; charset=utf-8")
                else:
                    self._send(404, b"not found", "text/plain; charset=utf-8")

        try:
            httpd = ThreadingHTTPServer(("", port), Handler)
        except OSError as e:
            logger.warning("web 状態サーバを起動できませんでした（継続）: %s", e)
            return
        self._httpd = httpd
        logger.info("web 状態サーバ起動: http://0.0.0.0:%d/", port)
        # poll_interval で停止フラグを定期確認する。
        while not self._stop.is_set():
            try:
                httpd.handle_request()
            except Exception as e:  # noqa: BLE001
                logger.debug("web 状態サーバで例外を無視: %s", e)
                if self._stop.is_set():
                    break
        try:
            httpd.server_close()
        except Exception:  # noqa: BLE001
            pass

    def _serve_setup_timeout(self):  # pragma: no cover - 補助
        pass

    def stop(self) -> None:
        self._stop.set()
        # handle_request のブロックを解くため自分宛にダミーリクエストを投げる。
        httpd = getattr(self, "_httpd", None)
        if httpd is not None:
            try:
                import socket as _socket

                with _socket.create_connection(
                    ("127.0.0.1", self.cfg.receiver.status.web_port), timeout=0.5
                ) as s:
                    s.sendall(b"GET / HTTP/1.0\r\n\r\n")
            except OSError:
                pass
        super().stop()


def _render_html(snap: dict) -> str:
    rows = []
    for s in snap["sources"]:
        state = "----" if not s["present"] else ("STALE" if s["stale"] else "ok")
        age = "-" if s["age_s"] is None else f"{s['age_s']:.2f}"
        rows.append(
            f"<tr><td>{s['name']}</td><td>{state}</td>"
            f"<td>{s['rate_hz']:.0f}</td><td>{age}</td><td>{s['frames']}</td></tr>"
        )
    head = ", ".join(str(v) for v in snap["dmx_head"])
    events = "".join(
        f"<li>系統{e['source']}: {e['kind']} @ t={e['t']:.3f}</li>"
        for e in snap.get("events", [])
    )
    st = snap["stats"]
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta http-equiv='refresh' content='1'>"
        "<title>receiver status</title></head><body>"
        "<h1>ArtNet receiver status</h1>"
        "<table border='1' cellpadding='4'>"
        "<tr><th>source</th><th>state</th><th>rate(Hz)</th><th>age(s)</th><th>frames</th></tr>"
        + "".join(rows)
        + "</table>"
        + f"<p>DMX ch1-8: [{head}]</p>"
        + f"<p>tx_frames={st['tx_frames']} rx_artdmx={st['rx_artdmx']} "
        + f"rx_packets={st['rx_packets']} rx_errors={st['rx_errors']}</p>"
        + "<h2>failover events</h2><ul>"
        + (events or "<li>(none)</li>")
        + "</ul></body></html>"
    )
