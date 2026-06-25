# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-06-26

Initial public release. Receive-only Art-Net → USB-DMX bridge.

### Added
- Art-Net (ArtDMX, UDP 6454) receiver that continuously streams DMX512 at a fixed `refresh_hz` (default 40Hz), holding the last values when reception stalls.
- USB-DMX output via Open DMX (generic FTDI, e.g. FT232R `0403:6001`, host-generated BREAK/MAB) and Enttec DMX USB Pro drivers.
- USB auto-detection with hotplug follow (`port: auto`): scans known DMX VID:PIDs, starts on connect, stops on disconnect.
- Optional 2-universe LTP (Latest Takes Precedence) merge with per-stream stale-timeout failover and recovery detection.
- `dry_run` safety mode and automatic fallback to dry-run when `pyserial` is unavailable.
- Status reporters: `console` (single-line), `web` (HTTP), and `none`.
- YAML configuration with per-key defaults and `--set KEY=VALUE` CLI overrides.
- `systemd/artnet2usbdmx.service` unit and `systemd/99-ftdi-dmx.rules` udev rule (FTDI `latency_timer` = 1ms).
- Documentation: `README.md` (Japanese, primary) and `README.en.md` (English).

[Unreleased]: https://github.com/4ltena/artnet2usbdmx/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/4ltena/artnet2usbdmx/releases/tag/v0.1.0
