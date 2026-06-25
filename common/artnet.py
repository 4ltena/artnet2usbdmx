"""Art-Net パケットのパース（受信側・自前実装）。

外部ライブラリを使わず自前実装する理由:
- 依存を最小化し Raspberry Pi 上で確実に動かすため。
- 受信側で扱うのは ArtDMX オペコードのみで、フォーマットも単純なため。

ハードウェア非依存・I/O 非依存の純粋ロジックのみをここに置く。

Art-Net のバイトオーダーに関する注意（実装上の罠）:
- OpCode は **リトルエンディアン**（下位バイトが先）。例: OpDmx(0x5000) -> 0x00, 0x50
- Length は **ビッグエンディアン**（上位バイトが先）。例: 512 -> 0x02, 0x00
この非対称性が混乱の元になりやすいので、struct のフォーマット指定を厳密に分けている。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

# --- 定数 -------------------------------------------------------------------

ART_NET_ID = b"Art-Net\x00"  # 8 bytes, NUL 終端込み
UDP_PORT = 6454

# OpCode（仕様上 16bit リトルエンディアンで格納される）
OP_DMX = 0x5000

DMX_CHANNELS = 512
ARTDMX_HEADER_LEN = 18  # ID(8)+OpCode(2)+ProtVer(2)+Seq(1)+Phys(1)+SubUni(1)+Net(1)+Len(2)


@dataclass
class ArtDmxPacket:
    """パース済み ArtDMX。

    universe は 15bit の Port-Address（Net<<8 | SubUni）を 1 つの整数として保持する。
    系統A/B の識別は呼び出し側がこの universe 値で行う（A=U, B=U+offset）。
    """

    universe: int
    sequence: int
    physical: int
    data: bytes  # DMX チャンネルデータ（最大 512 バイト）


def net_subuni_to_port_address(net: int, sub_uni: int) -> int:
    """(Net, SubUni) から 15bit Port-Address を合成する。"""
    return ((net & 0x7F) << 8) | (sub_uni & 0xFF)


def parse_opcode(raw: bytes) -> Optional[int]:
    """Art-Net ヘッダから OpCode を取り出す。Art-Net でなければ None。"""
    if len(raw) < 10:
        return None
    if raw[0:8] != ART_NET_ID:
        return None
    # OpCode はリトルエンディアン
    return struct.unpack_from("<H", raw, 8)[0]


def parse_artdmx(raw: bytes) -> Optional[ArtDmxPacket]:
    """ArtDMX をパースする。ArtDMX でない / 壊れている場合は None。

    堅牢性: Length フィールドが実データより大きい場合は実データ長に切り詰める。
    512 を超えるデータも 512 に切り詰める（受信側の安全策）。
    """
    if parse_opcode(raw) != OP_DMX:
        return None
    if len(raw) < ARTDMX_HEADER_LEN:
        return None

    sequence = raw[12]
    physical = raw[13]
    sub_uni = raw[14]
    net = raw[15]
    length = struct.unpack_from(">H", raw, 16)[0]  # Length: ビッグエンディアン

    available = len(raw) - ARTDMX_HEADER_LEN
    length = min(length, available, DMX_CHANNELS)
    data = bytes(raw[ARTDMX_HEADER_LEN:ARTDMX_HEADER_LEN + length])

    universe = net_subuni_to_port_address(net, sub_uni)
    return ArtDmxPacket(
        universe=universe,
        sequence=sequence,
        physical=physical,
        data=data,
    )
