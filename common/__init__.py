"""共通ロジック（ハードウェア非依存）。

- artnet: Art-Net(ArtDMX) パース
- dmx:    DMX フレーミングと USB-DMX 出力ドライバ
- merge:  LTP マージと stale 判定、フェイルオーバー検出
- config: 設定ローダ（YAML/TOML + CLI 上書き）
"""

from __future__ import annotations

from . import artnet, config, dmx, merge

__all__ = ["artnet", "config", "dmx", "merge"]
