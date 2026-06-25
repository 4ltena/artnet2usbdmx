"""設定ローダ（YAML / TOML）＋ CLI オーバーライド。

- YAML を主とする（Raspberry Pi OS で pyyaml が入手しやすいため）。
- TOML は Python 3.11+ の tomllib があれば対応（任意）。
- すべての項目に既定値があり、設定ファイルが無く / 空でも receiver は dry-run で起動できる。
- CLI からは "a.b.c=value" 形式のドットパスで個別上書きできる（型は自動推定）。

ハードウェア非依存。ユニットテスト対象。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Optional


# --- 設定スキーマ -----------------------------------------------------------

@dataclass
class ArtnetCfg:
    universe: int = 0          # 系統A の Port-Address
    stream_offset: int = 1     # 系統B = universe + offset
    port: int = 6454


@dataclass
class DmxCfg:
    # driver: open_dmx(FT232R 等の汎用FTDI) | enttec_pro | auto(検出デバイスから推定)
    driver: str = "open_dmx"
    # port: "auto" で USB を自動検出（照明=DMXデバイスを見つけたら送信、無ければ送信しない）。
    #       固定する場合は "/dev/ttyUSB0" 等を指定。
    port: str = "auto"
    dry_run: bool = False       # true で USB へ一切書き込まない（テスト時の安全装置・最優先）
    auto_rescan_s: float = 2.0  # 自動検出モードでの再走査間隔（秒）


@dataclass
class StatusCfg:
    mode: str = "console"      # console | web | none
    web_port: int = 8080
    interval_s: float = 0.5


@dataclass
class ReceiverCfg:
    stale_timeout_ms: int = 800
    refresh_hz: float = 40.0
    bind_ip: str = ""          # 受信バインド IP（""=全アドレス）
    dmx: DmxCfg = field(default_factory=DmxCfg)
    status: StatusCfg = field(default_factory=StatusCfg)


@dataclass
class LoggingCfg:
    level: str = "INFO"


@dataclass
class Config:
    artnet: ArtnetCfg = field(default_factory=ArtnetCfg)
    receiver: ReceiverCfg = field(default_factory=ReceiverCfg)
    logging: LoggingCfg = field(default_factory=LoggingCfg)

    # --- 便利プロパティ ---
    # 15bit Port-Address でマスクして返す。送信側も同じ 15bit にマスクして
    # ArtDMX を符号化するため、マスク前の生値で比較すると受信側が永久に一致せず
    # 系統が落ちる。送受で同一の正規化済み値を使うことで導通させる。
    @property
    def universe_a(self) -> int:
        return self.artnet.universe & 0x7FFF

    @property
    def universe_b(self) -> int:
        return (self.artnet.universe + self.artnet.stream_offset) & 0x7FFF

    @property
    def stale_timeout_s(self) -> float:
        return self.receiver.stale_timeout_ms / 1000.0


# --- 読み込み / マージ -------------------------------------------------------

def _load_raw(path: str) -> dict:
    """ファイルから生の dict を読む。拡張子で YAML/TOML を判別する。"""
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as f:
        raw_bytes = f.read()

    if ext in (".toml",):
        try:
            import tomllib  # Python 3.11+
        except ModuleNotFoundError:
            try:
                import tomli as tomllib  # type: ignore
            except ModuleNotFoundError as e:  # pragma: no cover
                raise RuntimeError(
                    "TOML を読むには Python 3.11+ か tomli が必要です"
                ) from e
        return tomllib.loads(raw_bytes.decode("utf-8")) or {}

    # 既定: YAML（.yaml/.yml/その他）
    import yaml
    return yaml.safe_load(raw_bytes.decode("utf-8")) or {}


def _apply_dict(obj: Any, data: dict) -> None:
    """dataclass インスタンス obj に dict をネスト適用する。

    未知のキーは無視する（前方互換）。型不一致はそのまま代入し、必要なら呼び出し側で検証。
    """
    if not is_dataclass(obj) or not isinstance(data, dict):
        return
    field_map = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in field_map:
            continue
        cur = getattr(obj, key)
        if is_dataclass(cur) and isinstance(value, dict):
            _apply_dict(cur, value)
        else:
            setattr(obj, key, value)


def _coerce(value: str) -> Any:
    """CLI 文字列を bool/int/float/str に推定変換する。"""
    low = value.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def _apply_override(cfg: Config, dotted: str, value: Any) -> None:
    """"a.b.c" 形式のパスで 1 項目を上書きする。"""
    parts = dotted.split(".")
    obj: Any = cfg
    for p in parts[:-1]:
        if not hasattr(obj, p):
            raise KeyError(f"未知の設定パス: {dotted}")
        obj = getattr(obj, p)
    leaf = parts[-1]
    if not hasattr(obj, leaf):
        raise KeyError(f"未知の設定パス: {dotted}")
    # 既存値の型に合わせて軽く変換
    cur = getattr(obj, leaf)
    if isinstance(cur, bool) and isinstance(value, str):
        value = _coerce(value)
    elif isinstance(cur, int) and not isinstance(cur, bool) and isinstance(value, str):
        value = int(value)
    elif isinstance(cur, float) and isinstance(value, str):
        value = float(value)
    setattr(obj, leaf, value)


def apply_overrides(cfg: Config, overrides: Optional[list[str]]) -> Config:
    """["a.b=1", "x.y=foo"] 形式の CLI 上書きを適用する。"""
    for item in overrides or []:
        if "=" not in item:
            raise ValueError(f"オーバーライドは key=value 形式: {item!r}")
        key, _, raw = item.partition("=")
        _apply_override(cfg, key.strip(), _coerce(raw.strip()))
    return cfg


def load_config(
    path: Optional[str] = None,
    overrides: Optional[list[str]] = None,
) -> Config:
    """設定を読み込む。path が None / 存在しなければ既定値のみで構築する。"""
    cfg = Config()
    if path and os.path.exists(path):
        _apply_dict(cfg, _load_raw(path))
    return apply_overrides(cfg, overrides)
