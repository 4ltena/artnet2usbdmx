"""DMX フレーミングと USB-DMX 出力ドライバ。

構成:
- フレーミング関数（純粋関数・ユニットテスト対象）:
    * enttec_pro_frame(): Enttec DMX USB Pro 系のシリアルフレームを組み立てる。
    * open_dmx_payload(): Open DMX/FTDI 用のラインペイロード（スタートコード+データ）を作る。
- 出力ドライバ（I/O を伴うため遅延 import）:
    * DmxOutput (基底)
    * EnttecProOutput  : pyserial で label6 フレームを送る。
    * OpenDmxOutput    : pyserial の break 制御で BREAK/MAB を生成して送る。
    * DryRunOutput     : ハードウェア未接続時。書き込みをスキップしログのみ。
    * AutoDmxOutput    : USB を自動検出し、照明(DMX)デバイスが在れば送信する。
                         無ければ送信せず、接続/切断をホットプラグ的に追従する。
- USB-DMX デバイス検出: list_dmx_candidates() が既知の DMX 用 USB(VID:PID)を走査する。

重要: pyserial / pyftdi は **メソッド内で遅延 import** する。
これにより、ライブラリ未インストールでもこのモジュールの import と dry-run は成功する
（USB 未接続でもクラッシュしないことを担保するため）。
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

DMX_CHANNELS = 512

# Enttec DMX USB Pro プロトコル
ENTTEC_START = 0x7E
ENTTEC_END = 0xE7
ENTTEC_LABEL_SEND_DMX = 6  # "Output Only Send DMX Packet Request"

# Open DMX / FTDI のシリアルパラメータ（250kbaud, 8N2）
OPEN_DMX_BAUD = 250000
OPEN_DMX_BYTESIZE = 8
OPEN_DMX_STOPBITS = 2  # 8N2
# BREAK は最低 88us、MAB は最低 8us。userspace では正確に出せないため余裕を持たせる。
OPEN_DMX_BREAK_S = 0.000176  # ~176us
OPEN_DMX_MAB_S = 0.000012    # ~12us


# --- 純粋なフレーミング関数 --------------------------------------------------

def _normalize_channels(channels: bytes, size: int = DMX_CHANNELS) -> bytes:
    """チャンネル列を 0..size に正規化する（不足は 0 埋め、超過は切り捨て）。"""
    if len(channels) >= size:
        return bytes(channels[:size])
    return bytes(channels) + b"\x00" * (size - len(channels))


def enttec_pro_frame(
    channels: bytes,
    label: int = ENTTEC_LABEL_SEND_DMX,
    start_code: int = 0x00,
    pad_to_512: bool = True,
) -> bytes:
    """Enttec DMX USB Pro 系の送信フレームを組み立てる。

    フレーム構造:
        0x7E | label | len_LSB | len_MSB | [start_code + channels...] | 0xE7
    - データ長 (len) = スタートコード 1 + チャンネル数。リトルエンディアン 2 バイト。
    - 512ch + スタートコードなら len = 513 -> LSB=0x01, MSB=0x02。
    """
    data = _normalize_channels(channels) if pad_to_512 else bytes(channels[:DMX_CHANNELS])
    payload = bytes([start_code & 0xFF]) + data
    n = len(payload)
    return (
        bytes([ENTTEC_START, label & 0xFF, n & 0xFF, (n >> 8) & 0xFF])
        + payload
        + bytes([ENTTEC_END])
    )


def open_dmx_payload(channels: bytes, start_code: int = 0x00) -> bytes:
    """Open DMX/FTDI でラインに流すバイト列（スタートコード + 512ch）。

    BREAK/MAB は別途タイミング制御で生成し、この戻り値を write() する。
    """
    return bytes([start_code & 0xFF]) + _normalize_channels(channels)


# --- 出力ドライバ ------------------------------------------------------------

class DmxOutput:
    """DMX 出力ドライバの基底クラス。"""

    def open(self) -> None:  # pragma: no cover - 基底は何もしない
        raise NotImplementedError

    def send(self, channels: bytes) -> None:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover
        pass

    def __enter__(self) -> "DmxOutput":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class DryRunOutput(DmxOutput):
    """ハードウェアを開かない。送信内容をデバッグログに出すだけ。

    USB 未接続や開発機での動作確認に使う。落ちないことが最優先。
    """

    def __init__(self, log_every: int = 40) -> None:
        self._count = 0
        self._log_every = max(1, log_every)
        self.last_frame: Optional[bytes] = None

    def open(self) -> None:
        logger.info("DMX dry-run モードで起動（実ハードウェアへは書き込まない）")

    def send(self, channels: bytes) -> None:
        self.last_frame = _normalize_channels(channels)
        self._count += 1
        if self._count % self._log_every == 0:
            head = " ".join(f"{b:3d}" for b in self.last_frame[:8])
            logger.debug("dry-run DMX frame #%d ch1-8=[%s]", self._count, head)

    def close(self) -> None:
        logger.info("DMX dry-run 終了（送信フレーム数=%d）", self._count)


class EnttecProOutput(DmxOutput):
    """Enttec DMX USB Pro 系。pyserial 経由で label6 フレームを送る。

    Pro はデバイス側ファームウェアが DMX タイミング（BREAK/MAB/40Hz リフレッシュ）を
    生成するため、ホストは label6 フレームを送るだけでよい。ホスト側の baudrate は
    USB-シリアル上の名目値で、DMX タイミングには直接影響しない。
    """

    def __init__(self, port: str, baudrate: int = 57600, write_timeout: float = 1.0) -> None:
        self.port = port
        self.baudrate = baudrate
        self.write_timeout = write_timeout
        self._ser = None

    def open(self) -> None:
        import serial  # 遅延 import: 未インストールでも dry-run は動かすため

        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            write_timeout=self.write_timeout,
        )
        logger.info("Enttec Pro を開きました: %s @ %d baud", self.port, self.baudrate)

    def send(self, channels: bytes) -> None:
        if self._ser is None:
            raise RuntimeError("EnttecProOutput.open() が呼ばれていません")
        self._ser.write(enttec_pro_frame(channels))

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None
            logger.info("Enttec Pro を閉じました: %s", self.port)


def _set_ftdi_latency_timer(port: str, value_ms: int = 1) -> bool:
    """FTDI (ftdi_sio) のレイテンシタイマを下げる（best-effort, Linux のみ）。

    FT232R など FTDI のレイテンシタイマ既定値は 16ms で、小さい書き込みがまとめて
    送られるため DMX のフレーム間隔が大きくジッタする。Linux では
    /sys/bus/usb-serial/devices/<tty>/latency_timer に 1 を書くと改善する。
    失敗しても例外を投げない（落とさない）。
    """
    import os

    name = os.path.basename(port)  # /dev/ttyUSB0 -> ttyUSB0
    sysfs = f"/sys/bus/usb-serial/devices/{name}/latency_timer"
    try:
        with open(sysfs, "w") as f:
            f.write(str(value_ms))
        logger.info("FTDI latency_timer を %dms に設定: %s", value_ms, sysfs)
        return True
    except OSError as e:
        logger.warning(
            "FTDI latency_timer を設定できませんでした (%s): %s。"
            "DMX フレームがジッタする場合は手動で 1 を書いてください。",
            sysfs, e,
        )
        return False


class OpenDmxOutput(DmxOutput):
    """Open DMX / FTDI 直結（FT232R USB UART を含む汎用 FTDI）。

    FT232R は Enttec Pro のような専用ファームを持たない汎用 USB-UART のため、
    ホスト側で BREAK/MAB と全スロット(スタートコード+512ch)を 250kbaud 8N2 で
    生成する必要がある。ここでは pyserial の break_condition で BREAK/MAB を作る。

    注意（タイミング）:
    - userspace の sleep ベースでは BREAK/MAB を正確に出せず、USB レイテンシの影響も
      受ける。FT232R では特に latency_timer(既定16ms)の影響が大きいため、open() 時に
      1ms へ下げることを試みる。
    - より厳密なタイミングが必要なら pyftdi のビットバンギングやカーネルドライバを検討。
    - ftdi_sio ドライバ配下では通常 /dev/ttyUSB0 として現れる。
    """

    def __init__(
        self,
        port: str,
        break_s: float = OPEN_DMX_BREAK_S,
        mab_s: float = OPEN_DMX_MAB_S,
        set_latency_timer: bool = True,
    ) -> None:
        self.port = port
        self.break_s = break_s
        self.mab_s = mab_s
        self.set_latency_timer = set_latency_timer
        self._ser = None

    def open(self) -> None:
        import serial  # 遅延 import

        if self.set_latency_timer:
            _set_ftdi_latency_timer(self.port, 1)  # best-effort
        self._ser = serial.Serial(
            port=self.port,
            baudrate=OPEN_DMX_BAUD,
            bytesize=OPEN_DMX_BYTESIZE,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_TWO,  # 8N2
        )
        logger.info("Open DMX/FTDI (FT232R 等) を開きました: %s @ %d baud 8N2", self.port, OPEN_DMX_BAUD)

    def send(self, channels: bytes) -> None:
        if self._ser is None:
            raise RuntimeError("OpenDmxOutput.open() が呼ばれていません")
        ser = self._ser
        # BREAK -> MAB -> スタートコード+データ
        ser.break_condition = True
        time.sleep(self.break_s)
        ser.break_condition = False
        time.sleep(self.mab_s)
        ser.write(open_dmx_payload(channels))

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None
            logger.info("Open DMX を閉じました: %s", self.port)


# --- USB-DMX(照明) デバイスの自動検出 ---------------------------------------

# 既知の DMX/照明用 USB-シリアル変換 (VID, PID)。
# FT232R(0403:6001) は Open DMX / Enttec DMX USB Pro など照明用途で広く使われる。
# 汎用 FTDI のため厳密には DMX 専用と断定できないが、本系の照明 I/F はこれに該当する。
KNOWN_DMX_USB_IDS = {
    (0x0403, 0x6001),  # FTDI FT232R / FT245R（Open DMX, Enttec DMX USB Pro 等）
    (0x0403, 0x6010),  # FTDI FT2232
    (0x0403, 0x6011),  # FTDI FT4232
    (0x0403, 0x6014),  # FTDI FT232H
    (0x0403, 0x6015),  # FTDI FT-X（FT230X/FT231X）
    (0x16C0, 0x05DC),  # uDMX (Anyma)
}


def _is_dmx_usb(vid, pid) -> bool:
    return vid is not None and (vid, pid) in KNOWN_DMX_USB_IDS


def _match_dmx(ports) -> list:
    """list_ports.comports() 相当のリストから DMX 候補を抽出する（純粋関数）。

    返り値: [(device_path, vid, pid, description), ...]
    """
    out = []
    for p in ports:
        vid = getattr(p, "vid", None)
        pid = getattr(p, "pid", None)
        if _is_dmx_usb(vid, pid):
            desc = getattr(p, "product", None) or getattr(p, "description", None) or ""
            out.append((p.device, vid, pid, desc))
    return out


def list_dmx_candidates() -> list:
    """接続中の USB シリアルから照明(DMX)インターフェイス候補を返す。

    pyserial 未導入なら空リスト（落ちない）。
    """
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return _match_dmx(list_ports.comports())


def _driver_for_description(desc: str, default: str = "open_dmx") -> str:
    """検出デバイスの説明文字列からドライバを推定する。

    Enttec DMX USB Pro は専用ラベルプロトコル、それ以外の FTDI は Open DMX 方式。
    """
    u = (desc or "").upper()
    if "PRO" in u or "ENTTEC" in u:
        return "enttec_pro"
    return default


def _make_serial_output(driver: str, port: str) -> DmxOutput:
    """シリアル実ドライバ（Enttec Pro / Open DMX）を生成する。"""
    driver = (driver or "").lower()
    if driver in ("enttec_pro", "enttec", "pro"):
        return EnttecProOutput(port)
    if driver in ("open_dmx", "opendmx", "ftdi", "ft232r", "udmx"):
        return OpenDmxOutput(port)
    raise ValueError(f"未知の DMX ドライバ: {driver!r}")


class AutoDmxOutput(DmxOutput):
    """USB を自動検出し、照明(DMX)デバイスが在れば送信する出力。

    - 起動時および送信時に既知の DMX USB(VID:PID)を走査する。検出したら driver
      （"auto" の場合は説明文字列から Enttec Pro / Open DMX を推定）で開いて送信する。
    - デバイスが無い間は送信しない。接続されたら自動で開く（rescan は rate-limit）。
    - 送信エラー（抜けた等）なら閉じて再走査に戻る（ホットプラグ追従）。
    """

    def __init__(self, driver: str = "auto", rescan_s: float = 2.0, now=time.monotonic) -> None:
        self._driver = driver or "auto"
        self._rescan_s = rescan_s
        self._now = now
        self._out: Optional[DmxOutput] = None
        self._port: Optional[str] = None
        self._last_scan: Optional[float] = None

    def open(self) -> None:
        logger.info(
            "DMX 自動検出モード（USB を監視し、照明デバイスを検出したら送信します）"
        )
        self._scan_and_open(force=True)

    def _scan_and_open(self, force: bool = False) -> None:
        t = self._now()
        if (
            not force
            and self._last_scan is not None
            and (t - self._last_scan) < self._rescan_s
        ):
            return
        self._last_scan = t
        cands = list_dmx_candidates()
        if not cands:
            return
        port, vid, pid, desc = cands[0]
        driver = self._driver
        if driver == "auto":
            driver = _driver_for_description(desc)
        try:
            out = _make_serial_output(driver, port)
            out.open()
        except Exception as e:  # noqa: BLE001  開けなくても落とさない
            logger.warning("照明デバイス候補 %s を開けません: %s", port, e)
            return
        self._out = out
        self._port = port
        logger.info(
            "照明(DMX)デバイスを検出: %s (USB %04x:%04x %s, driver=%s) → 送信開始",
            port, vid, pid or 0, desc, driver,
        )

    def send(self, channels: bytes) -> None:
        if self._out is None:
            self._scan_and_open()
            if self._out is None:
                return  # 照明デバイス無し → 何も送信しない
        try:
            self._out.send(channels)
        except Exception as e:  # noqa: BLE001  抜けた等 → 再検出へ
            logger.warning("DMX 送出失敗（デバイス切断?）: %s → 再検出に戻ります", e)
            self._close_out()

    def _close_out(self) -> None:
        if self._out is not None:
            try:
                self._out.close()
            except Exception:  # noqa: BLE001
                pass
        self._out = None
        self._port = None

    def close(self) -> None:
        self._close_out()
        logger.info("DMX 自動検出モードを終了")

    @property
    def active_port(self) -> Optional[str]:
        """現在送信中のポート（未接続なら None）。状態表示用。"""
        return self._port


def make_dmx_output(
    driver: str,
    port: str,
    dry_run: bool = False,
    rescan_s: float = 2.0,
) -> DmxOutput:
    """設定からドライバを生成するファクトリ。

    優先順位:
    1. dry_run=True → DryRunOutput（テスト時など照明へ一切送信しない安全装置）。
    2. pyserial 未インストール → DryRunOutput（落ちない）。
    3. port=="auto" もしくは driver=="auto" → AutoDmxOutput（USB 自動検出）。
    4. それ以外 → 固定ポートの実ドライバ。
    """
    if dry_run:
        return DryRunOutput()

    try:
        import serial  # noqa: F401  存在確認のみ
    except ImportError:
        logger.warning("pyserial 未インストールのため dry-run にフォールバックします")
        return DryRunOutput()

    if (port or "").lower() == "auto" or (driver or "").lower() == "auto":
        # driver が具体指定なら検出後その driver を使う。"auto"/空なら説明から推定。
        d = driver if (driver or "").lower() not in ("", "auto") else "auto"
        return AutoDmxOutput(driver=d, rescan_s=rescan_s)

    return _make_serial_output(driver, port)
