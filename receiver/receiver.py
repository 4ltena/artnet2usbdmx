"""受信/マージ/送信の中核（ArtNet -> USB-DMX）。

スレッド構成:
- RX スレッド (_rx_loop): UDP を待ち受け、ArtDMX をパースして系統A/B に振り分け、
  受信時刻(time.monotonic)とともに LtpMerger.update() を呼ぶ。
- TX スレッド (_tx_loop): refresh_hz の固定 tick でマージ結果を USB-DMX へ送出する。
  受信が途切れても merge() が直近値を返すため、出力は途切れない。
- 監視 (_tx_loop 内): FailoverTracker で系統の stale/復帰を検出し、構造化ログに残す。

堅牢性:
- 不正パケット・パースエラーは握りつぶす（DEBUG ログ）。
- ソケットエラー / ネットワーク断は recv をリトライし、クラッシュさせない。
- USB/pyserial 未接続でも make_dmx_output が DryRun に落ちるため起動・継続できる。
- 送信中の I/O 例外も握りつぶし、次 tick で再送する（必要なら再オープンを試みる）。

時刻は time.monotonic を基準にし、TX は次 tick を絶対時刻で補正してドリフトを防ぐ。
"""

from __future__ import annotations

import errno
import logging
import socket
import threading
import time
from typing import Optional

from common.config import Config
from common.dmx import DmxOutput, make_dmx_output
from common.merge import FailoverTracker, LtpMerger

logger = logging.getLogger(__name__)

# UDP 受信バッファサイズ（ArtDMX は最大 ~530 バイトだが余裕を持たせる）
_RECV_BUFSIZE = 2048
# recv のタイムアウト（停止フラグを定期的に確認するため）
_RECV_TIMEOUT_S = 0.5


class Receiver:
    """ArtNet 受信 → LTP マージ → USB-DMX 連続送出を行う中核クラス。

    使い方:
        rx = Receiver(cfg)
        rx.start()
        ...
        rx.stop()
    あるいは run() でブロッキング起動（SIGINT/SIGTERM は __main__ 側で stop() を呼ぶ）。
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        # 系統A を先に渡す（LtpMerger のタイブレークは順序依存。A 優先）。
        self.merger = LtpMerger(
            ["A", "B"],
            stale_timeout=cfg.stale_timeout_s,
        )
        self.failover = FailoverTracker(["A", "B"])

        self._dmx: Optional[DmxOutput] = None
        self._sock: Optional[socket.socket] = None

        self._stop = threading.Event()
        self._rx_thread: Optional[threading.Thread] = None
        self._tx_thread: Optional[threading.Thread] = None

        # 統計（状態表示用。読み取りは概算で良いので Lock は省略）。
        self.rx_packets = 0          # 受信した UDP データグラム総数
        self.rx_artdmx = 0           # ArtDMX として取り込めた数
        self.rx_ignored_universe = 0  # 対象外ユニバースで無視した数
        self.rx_errors = 0           # パース/受信で握りつぶしたエラー数
        self.tx_frames = 0           # DMX 送出フレーム数

        # フェイルオーバー履歴（状態表示用。直近のみ保持）。
        self._events_lock = threading.Lock()
        self._events: list = []
        self._events_max = 50

    # --- ライフサイクル -----------------------------------------------------

    def start(self) -> None:
        """ソケットと DMX 出力を開き、RX/TX スレッドを起動する。"""
        self._open_dmx()
        self._open_socket()

        self._rx_thread = threading.Thread(
            target=self._rx_loop, name="artnet-rx", daemon=True
        )
        self._tx_thread = threading.Thread(
            target=self._tx_loop, name="dmx-tx", daemon=True
        )
        self._rx_thread.start()
        self._tx_thread.start()
        logger.info(
            "受信開始: universe_a=%d universe_b=%d port=%d bind=%r refresh=%.1fHz",
            self.cfg.universe_a,
            self.cfg.universe_b,
            self.cfg.artnet.port,
            self.cfg.receiver.bind_ip or "(all)",
            self.cfg.receiver.refresh_hz,
        )
        if self.cfg.universe_a == self.cfg.universe_b:
            logger.warning(
                "universe_a==universe_b (stream_offset=%d)。系統B が受信されず二重化に"
                "なりません。stream_offset を 1 以上に設定してください。",
                self.cfg.artnet.stream_offset,
            )

    def stop(self) -> None:
        """停止フラグを立て、スレッドを join し、ソケット/DMX をクローズする。"""
        self._stop.set()
        # ソケットを閉じて recv のブロックを解く（timeout 併用だが即応のため）。
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        for th in (self._rx_thread, self._tx_thread):
            if th is not None and th.is_alive():
                th.join(timeout=2.0)
        if self._dmx is not None:
            try:
                self._dmx.close()
            except Exception as e:  # noqa: BLE001  クローズ失敗で落とさない
                logger.warning("DMX クローズ時の例外を無視: %s", e)
        logger.info(
            "受信停止: rx_pkts=%d artdmx=%d tx_frames=%d errors=%d",
            self.rx_packets, self.rx_artdmx, self.tx_frames, self.rx_errors,
        )

    def run(self) -> None:
        """ブロッキング起動。stop() が呼ばれるまで待機する。"""
        self.start()
        try:
            while not self._stop.is_set():
                self._stop.wait(0.5)
        finally:
            self.stop()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # --- セットアップ -------------------------------------------------------

    def _open_dmx(self) -> None:
        dmx_cfg = self.cfg.receiver.dmx
        self._dmx = make_dmx_output(
            dmx_cfg.driver,
            dmx_cfg.port,
            dry_run=dmx_cfg.dry_run,
            rescan_s=getattr(dmx_cfg, "auto_rescan_s", 2.0),
        )
        try:
            self._dmx.open()
        except Exception as e:  # noqa: BLE001  実機未接続でも継続する
            # USB 未接続等で open に失敗しても落とさず DryRun に退避する。
            logger.warning(
                "DMX 出力の open に失敗したため dry-run に切替えます: %s", e
            )
            from common.dmx import DryRunOutput

            self._dmx = DryRunOutput()
            try:
                self._dmx.open()
            except Exception:  # noqa: BLE001  DryRun の open は基本失敗しない
                pass

    def _open_socket(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # ブロードキャスト受信のため SO_BROADCAST を有効化。
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except OSError as e:
            logger.warning("SO_BROADCAST 設定に失敗（継続）: %s", e)
        sock.settimeout(_RECV_TIMEOUT_S)

        bind_ip = self.cfg.receiver.bind_ip or ""
        sock.bind((bind_ip, self.cfg.artnet.port))
        self._sock = sock
        logger.info(
            "UDP 受信ソケットを開きました: bind=%r port=%d",
            bind_ip or "(all)", self.cfg.artnet.port,
        )

    # --- RX スレッド --------------------------------------------------------

    def _rx_loop(self) -> None:
        """UDP を受信し、ArtDMX を系統A/B に振り分けて merger に流す。"""
        # 遅延 import（純粋ロジックだが起動順を common 側に揃える）。
        from common import artnet

        universe_a = self.cfg.universe_a
        universe_b = self.cfg.universe_b

        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                break
            try:
                raw, _addr = sock.recvfrom(_RECV_BUFSIZE)
            except socket.timeout:
                continue  # 停止フラグ確認のための周期的タイムアウト
            except OSError as e:
                if self._stop.is_set():
                    break
                # WiFi 断/復帰やソケット一時障害でクラッシュさせない。
                # EBADF はクローズ直後に起こりうる（停止時）。
                if e.errno == errno.EBADF:
                    break
                self.rx_errors += 1
                logger.debug("recvfrom エラー（継続）: %s", e)
                # 連続エラー時に CPU を焼かないよう軽くスリープ。
                self._stop.wait(0.05)
                continue

            self.rx_packets += 1
            t = time.monotonic()
            try:
                pkt = artnet.parse_artdmx(raw)
            except Exception as e:  # noqa: BLE001  パースは None 返す設計だが念のため握る
                self.rx_errors += 1
                logger.debug("ArtDMX パース例外を無視: %s", e)
                continue
            if pkt is None:
                # Art-Net でない / ArtDMX でない / 破損 -> 握りつぶす。
                continue

            if pkt.universe == universe_a:
                source = "A"
            elif pkt.universe == universe_b:
                source = "B"
            else:
                self.rx_ignored_universe += 1
                continue

            self.rx_artdmx += 1
            self.merger.update(source, pkt.data, t)

        logger.debug("RX ループ終了")

    # --- TX スレッド --------------------------------------------------------

    def _tx_loop(self) -> None:
        """refresh_hz の固定 tick でマージ結果を送出し、フェイルオーバーを監視する。"""
        rate = self.cfg.receiver.refresh_hz
        if rate <= 0:
            # 0 や負値（--set の誤設定など）。period が負だと next_tick 補正が
            # 常にリセットされ self._stop.wait() を一切呼ばない CPU ビジーループになる。
            logger.warning("refresh_hz=%s は無効です。40Hz にフォールバックします。", rate)
            rate = 40.0
        period = 1.0 / rate
        next_tick = time.monotonic()

        while not self._stop.is_set():
            t = time.monotonic()

            # マージ -> 送出（全系統 stale でも直近値が返るので途切れない）。
            try:
                frame = self.merger.merge(t)
            except Exception as e:  # noqa: BLE001  純粋ロジックだが保険
                logger.debug("merge 例外を無視: %s", e)
                frame = None

            if frame is not None and self._dmx is not None:
                try:
                    self._dmx.send(frame)
                    self.tx_frames += 1
                except Exception as e:  # noqa: BLE001  USB 抜け等で落とさない
                    logger.warning("DMX 送出に失敗（次 tick で再試行）: %s", e)

            # フェイルオーバー検出 -> 構造化ログ。
            try:
                statuses = self.merger.status(t)
                events = self.failover.observe(statuses, t)
                for ev in events:
                    self._record_event(ev)
                    if ev.kind == "stale":
                        logger.warning(
                            "フェイルオーバー: 系統%s が stale になりました (t=%.3f)",
                            ev.source, ev.t,
                        )
                    else:
                        logger.info(
                            "復帰: 系統%s が回復しました (t=%.3f)",
                            ev.source, ev.t,
                        )
            except Exception as e:  # noqa: BLE001
                logger.debug("フェイルオーバー監視で例外を無視: %s", e)

            # 次 tick まで待つ（絶対時刻補正でドリフトを抑える）。
            next_tick += period
            sleep_s = next_tick - time.monotonic()
            if sleep_s > 0:
                self._stop.wait(sleep_s)
            else:
                # 処理が tick を超過した（遅延）。次 tick を現在時刻基準に戻す。
                next_tick = time.monotonic()

        logger.debug("TX ループ終了")

    # --- フェイルオーバー履歴 ----------------------------------------------

    def _record_event(self, ev) -> None:
        with self._events_lock:
            self._events.append(ev)
            if len(self._events) > self._events_max:
                del self._events[: len(self._events) - self._events_max]

    def recent_events(self, limit: int = 10) -> list:
        """直近のフェイルオーバーイベントを新しい順に返す（状態表示用）。"""
        with self._events_lock:
            return list(reversed(self._events[-limit:]))

    # --- 状態スナップショット（状態表示用） --------------------------------

    def snapshot(self) -> dict:
        """現在の状態を dict で返す（console/web 共通の素材）。"""
        t = time.monotonic()
        statuses = self.merger.status(t)
        merged = self.merger.merge(t)
        return {
            "t": t,
            "sources": [
                {
                    "name": s.name,
                    "present": s.present,
                    "stale": s.stale,
                    "age_s": s.age_s,
                    "frames": s.frames,
                    "rate_hz": s.rate_hz,
                }
                for s in statuses
            ],
            "dmx_head": list(merged[:8]),
            "stats": {
                "rx_packets": self.rx_packets,
                "rx_artdmx": self.rx_artdmx,
                "rx_ignored_universe": self.rx_ignored_universe,
                "rx_errors": self.rx_errors,
                "tx_frames": self.tx_frames,
                "dmx_out": type(self._dmx).__name__ if self._dmx is not None else None,
                "dmx_port": getattr(self._dmx, "active_port", None),
            },
            "events": [
                {"t": e.t, "source": e.source, "kind": e.kind}
                for e in self.recent_events(10)
            ],
        }
