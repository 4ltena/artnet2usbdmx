"""LTP（Latest Takes Precedence）マージと stale（途絶）判定。

設計（受信側の中核ロジック）:
- 複数系統（A/B）の DMX フレームを ch 単位でマージする。
- 各 ch について「最後にその ch の値を更新した系統」の値を採用する = LTP。
- 各系統に stale タイムアウト（既定 800ms）。未更新系統はマージ対象から除外する。
- 全系統が stale のときは直近のマージ結果を保持する（出力を途切れさせない）。

スレッド安全性:
- update() は受信スレッドから、merge()/status() は送信スレッドから呼ばれうるため、
  内部状態は Lock で保護する。

テスタビリティ:
- 時刻取得は now コールバックを注入可能。テストでは t を明示指定する。
- ハードウェア・ネットワーク非依存の純粋ロジック。
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Optional

DMX_CHANNELS = 512


@dataclass
class SourceStatus:
    """系統ごとの可視化用ステータス。"""

    name: str
    present: bool          # 一度でもフレームを受信したか
    stale: bool            # stale タイムアウトを超えているか
    last_seen: Optional[float]
    age_s: Optional[float]  # 最終受信からの経過秒
    frames: int
    rate_hz: float          # 直近 1 秒のパケットレート


class _SourceState:
    """系統 1 つ分の内部状態。"""

    __slots__ = ("name", "values", "changed_at", "last_seen", "frames", "_arrivals")

    def __init__(self, name: str, channels: int) -> None:
        self.name = name
        self.values = bytearray(channels)
        self.changed_at = [float("-inf")] * channels  # 各 ch の最終変更時刻
        self.last_seen: Optional[float] = None
        self.frames = 0
        self._arrivals: Deque[float] = deque(maxlen=128)

    def update(self, data: bytes, t: float) -> None:
        n = len(self.values)
        first = self.frames == 0
        for ch in range(min(len(data), n)):
            v = data[ch]
            if first or self.values[ch] != v:
                self.values[ch] = v
                self.changed_at[ch] = t
        # data が n 未満のチャンネルは前回値を保持（変更扱いしない）
        self.last_seen = t
        self.frames += 1
        self._arrivals.append(t)

    def rate_hz(self, t: float, window: float = 1.0) -> float:
        cutoff = t - window
        cnt = sum(1 for a in self._arrivals if a >= cutoff)
        return cnt / window

    def is_stale(self, t: float, timeout: float) -> bool:
        if self.last_seen is None:
            return True
        return (t - self.last_seen) > timeout


class LtpMerger:
    """2 系統以上の DMX を LTP マージするエンジン。

    sources の順序はタイブレークに使う（同時刻に同 ch を変更した場合、先に列挙した
    系統を優先する＝決定的に振る舞う）。系統A を先に渡すこと。
    """

    def __init__(
        self,
        sources: list[str],
        stale_timeout: float = 0.8,
        channels: int = DMX_CHANNELS,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        if not sources:
            raise ValueError("sources は 1 つ以上必要です")
        self._channels = channels
        self._stale_timeout = stale_timeout
        self._now = now
        self._lock = threading.Lock()
        self._sources: dict[str, _SourceState] = {
            name: _SourceState(name, channels) for name in sources
        }
        self._order: list[str] = list(sources)
        self._last_merged = bytearray(channels)

    # --- 受信スレッドから呼ぶ ---

    def update(self, source: str, data: bytes, t: Optional[float] = None) -> None:
        """系統 source の最新フレームを取り込む。

        未知の系統名は無視する（想定外ユニバースの混入を防ぐ）。
        """
        if t is None:
            t = self._now()
        with self._lock:
            st = self._sources.get(source)
            if st is None:
                return
            st.update(data, t)

    # --- 送信スレッドから呼ぶ ---

    def merge(self, t: Optional[float] = None) -> bytes:
        """現時点のマージ結果（512ch）を返す。

        non-stale な系統の中から、各 ch について最後に変更した系統の値を採る。
        全系統 stale の場合は直近のマージ結果を保持して返す。
        """
        if t is None:
            t = self._now()
        with self._lock:
            active = [
                self._sources[name]
                for name in self._order
                if not self._sources[name].is_stale(t, self._stale_timeout)
                and self._sources[name].frames > 0
            ]
            if not active:
                # 全系統 stale -> 直近の出力を保持
                return bytes(self._last_merged)

            result = bytearray(self._channels)
            for ch in range(self._channels):
                best = None
                best_t = float("-inf")
                for st in active:  # _order 順 -> 同時刻なら先頭系統が勝つ
                    ct = st.changed_at[ch]
                    if ct > best_t:
                        best_t = ct
                        best = st
                if best is not None:
                    result[ch] = best.values[ch]
            self._last_merged = result
            return bytes(result)

    # --- 可視化 ---

    def status(self, t: Optional[float] = None) -> list[SourceStatus]:
        if t is None:
            t = self._now()
        with self._lock:
            out = []
            for name in self._order:
                st = self._sources[name]
                age = None if st.last_seen is None else (t - st.last_seen)
                out.append(
                    SourceStatus(
                        name=name,
                        present=st.frames > 0,
                        stale=st.is_stale(t, self._stale_timeout),
                        last_seen=st.last_seen,
                        age_s=age,
                        frames=st.frames,
                        rate_hz=st.rate_hz(t),
                    )
                )
            return out

    @property
    def stale_timeout(self) -> float:
        return self._stale_timeout

    @property
    def sources(self) -> list[str]:
        return list(self._order)


@dataclass
class FailoverEvent:
    """フェイルオーバー / 復帰イベント（ログ用）。"""

    t: float
    source: str
    kind: str  # "stale" | "recovered"


class FailoverTracker:
    """系統の stale/復帰の遷移を検出してイベント列にする。

    merger.status() を定期的に渡すと、状態が変化した系統についてイベントを返す。
    純粋ロジック（時刻はステータスに含まれる値を使う）。
    """

    def __init__(self, sources: list[str]) -> None:
        # 初期状態は「健全」とみなす（最初の stale で初めてイベントを出す）
        self._stale_state: dict[str, bool] = {s: False for s in sources}

    def observe(self, statuses: list[SourceStatus], t: float) -> list[FailoverEvent]:
        events: list[FailoverEvent] = []
        for s in statuses:
            prev = self._stale_state.get(s.name, False)
            # まだ一度も受信していない系統は「健全/stale」判定の対象にしない
            if not s.present:
                continue
            if s.stale and not prev:
                events.append(FailoverEvent(t=t, source=s.name, kind="stale"))
                self._stale_state[s.name] = True
            elif not s.stale and prev:
                events.append(FailoverEvent(t=t, source=s.name, kind="recovered"))
                self._stale_state[s.name] = False
        return events
