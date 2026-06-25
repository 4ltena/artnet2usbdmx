"""受信側（ArtNet -> USB-DMX）。

役割:
- ArtNet(UDP) を受信し、ArtDMX を系統A/B に振り分けて LTP マージする。
- マージ後の DMX を USB-DMX へ refresh_hz で連続送出する（受信が途切れても直近値を保持）。
- フェイルオーバー（系統 stale/復帰）を検出してログに残し、状態表示する。

スレッド構成:
- 受信スレッド: UDP recv -> parse_artdmx -> LtpMerger.update()
- 送信スレッド: refresh_hz tick -> LtpMerger.merge() -> DmxOutput.send()
- 状態スレッド: console/web の状態表示（status.py）

LtpMerger は内部 Lock 済みなのでスレッド間共有して安全。
"""

from __future__ import annotations

from . import receiver, status

__all__ = ["receiver", "status"]
