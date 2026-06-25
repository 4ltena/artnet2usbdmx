"""receiver の CLI エントリポイント。

    python -m receiver [--config PATH] [--set KEY=VALUE ...] [--log-level LEVEL] [--dry-run]

- --config: 設定 YAML/TOML のパス（省略時は既定値のみ。dry-run で起動可）。
- --set:    "a.b.c=value" 形式の個別上書き（複数指定可）。load_config の overrides に渡す。
- --log-level: logging のレベル（DEBUG/INFO/WARNING/ERROR）。--config の logging.level を上書き。
- --dry-run: receiver.dmx.dry_run=true 相当（USB へ書き込まずログのみ）。

SIGINT/SIGTERM で安全に停止する（スレッド join、ソケット/シリアルをクローズ）。
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from typing import Optional

from common.config import load_config

from .receiver import Receiver
from .status import make_status_reporter


def _parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m receiver",
        description="ArtNet 受信 → LTP マージ → USB-DMX 連続送出",
    )
    p.add_argument("--config", default=None, help="設定ファイル（YAML/TOML）のパス")
    p.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="設定の個別上書き（例: --set receiver.dmx.dry_run=true）。複数指定可。",
    )
    p.add_argument(
        "--log-level",
        default=None,
        help="ログレベル（DEBUG/INFO/WARNING/ERROR）。設定より優先。",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="USB へ書き込まずログのみ（receiver.dmx.dry_run=true 相当）。",
    )
    return p.parse_args(argv)


def _setup_logging(level_name: str) -> None:
    level = getattr(logging, (level_name or "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
    )


def main(argv: Optional[list] = None) -> int:
    args = _parse_args(argv)

    overrides = list(args.overrides)
    if args.dry_run:
        overrides.append("receiver.dmx.dry_run=true")
    if args.log_level:
        overrides.append("logging.level=" + args.log_level)

    try:
        cfg = load_config(args.config, overrides=overrides)
    except Exception as e:  # noqa: BLE001  設定エラーはメッセージを出して終了
        print(f"設定の読み込みに失敗しました: {e}", file=sys.stderr)
        return 2

    _setup_logging(cfg.logging.level)
    logger = logging.getLogger("receiver")

    receiver = Receiver(cfg)
    status = make_status_reporter(receiver, cfg)

    # SIGINT/SIGTERM で安全停止。ハンドラはイベントを立てるだけにする。
    stop_event = threading.Event()

    def _handle_signal(signum, _frame):
        logger.info("シグナル %s を受信。停止します。", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (ValueError, AttributeError):  # 一部環境で未対応でも継続
        pass

    receiver.start()
    status.start()
    try:
        while not stop_event.is_set():
            stop_event.wait(0.5)
    finally:
        status.stop()
        receiver.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
