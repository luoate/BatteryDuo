#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Aula A7 Pro (2.4 GHz) battery listener for Windows 11.

Device: VID:PID 38A6:0001, HID Usage Page FF90, interface 0（实测枚举）。
支持被动监听 FF90 输入报告，并可由用户手动启用实验性的定时主动查询。
查询协议经单次实机验证：64 字节 Output Report ``55 65 01 00 ... 00``，
随后收到 ``55 65 50 02``（80%，未充电）。单次测试尚不能证明每次都会响应。
默认不主动写入；托盘菜单启用后最短每 60 秒发送一次。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from typing import Any

from battery_types import BatteryQueryError, BatteryStatus

VID = 0x38A6
PID = 0x0001
USAGE_PAGE = 0xFF90
REPORT_ID = 0x55
MARKER = 0x65
READ_LENGTH = 64
QUERY = bytes([0x55, 0x65, 0x01] + [0x00] * 61)
MIN_POLL_INTERVAL_S = 60.0
MIN_MANUAL_GAP_S = 10.0
CHARGING_MODES = {0x02: False, 0x03: True}
NAME = "Aula A7 Pro"


def find_target(hid: Any) -> dict[str, Any]:
    """Return the vendor collection only; never open the generic mouse interface."""
    try:
        devices = hid.enumerate(VID, PID)
    except Exception as exc:
        raise BatteryQueryError(f"枚举 HID 接收器失败：{exc}") from exc
    matches = [d for d in devices if d.get("usage_page") == USAGE_PAGE
               and d.get("usage") == 1 and d.get("path")
               and d.get("interface_number") in (None, 0)]
    if not matches:
        raise BatteryQueryError(
            "未找到目标接口 38A6:0001 / Usage Page FF90。请确认 A7 Pro 的接收器已插入。"
        )
    return matches[0]


def parse_report(data: bytes) -> BatteryStatus | None:
    """Validate one input report. None means "not a battery report, keep listening"."""
    if len(data) < 4 or data[0] != REPORT_ID or data[1] != MARKER:
        return None
    percent = data[2]
    if percent > 100:
        raise BatteryQueryError(
            f"电量字段超出 0–100 范围（读到 {percent}）。原始上报：{data.hex(' ').upper()}"
        )
    mode = data[3]
    if mode == 0x1A and percent == 0:
        # 实际抓包：55 65 00 1A 为关机消息，绝不能把它显示成 0% 电量。
        raise BatteryQueryError("检测到耳机关闭或断开连接（FF90 关机状态消息）。")
    if mode not in CHARGING_MODES:
        raise BatteryQueryError(
            f"未知的充电状态字节 0x{mode:02X}。原始上报：{data.hex(' ').upper()}"
        )
    return BatteryStatus(percent, None, CHARGING_MODES[mode], data)


class AulaA7Pro:
    """在同一个 HID 句柄上监听输入；实验性主动查询可选，避免接口竞争。"""

    name = NAME
    push = True  # 即使启用主动查询，也始终监听耳机自己的实时事件。
    charge_label = "正在充电"

    def __init__(self, active_poll: bool = False, interval_s: int = 60) -> None:
        self._dev: Any = None
        self._settings_lock = threading.Lock()
        self._active_poll = bool(active_poll)
        self._interval_s = max(float(interval_s), MIN_POLL_INTERVAL_S)
        self._manual_refresh = threading.Event()
        self._last_query_at = float("-inf")

    def set_active_poll(self, enabled: bool) -> None:
        """从托盘主线程调用；关闭后不再发送 Output Report。"""
        with self._settings_lock:
            self._active_poll = bool(enabled)
            if enabled:
                self._manual_refresh.set()
            else:
                self._manual_refresh.clear()

    def set_interval(self, seconds: int) -> None:
        with self._settings_lock:
            self._interval_s = max(float(seconds), MIN_POLL_INTERVAL_S)

    def request_refresh(self) -> None:
        with self._settings_lock:
            if self._active_poll:
                self._manual_refresh.set()

    def _query_due(self) -> bool:
        """每分钟至多自动查询一次；手动刷新至少相隔 10 秒。"""
        with self._settings_lock:
            if not self._active_poll:
                return False
            now = time.monotonic()
            elapsed = now - self._last_query_at
            manual = self._manual_refresh.is_set()
            if elapsed < MIN_MANUAL_GAP_S:
                return False
            if not manual and elapsed < self._interval_s:
                return False
            self._manual_refresh.clear()
            # 写入前记录时间：写失败也不会在下一循环高频重试。
            self._last_query_at = now
            return True

    def _ensure_open(self, hid: Any) -> Any:
        if self._dev is None:
            desc = find_target(hid)
            try:
                dev = hid.device()
                dev.open_path(desc["path"])
                dev.set_nonblocking(False)
            except (OSError, ValueError, RuntimeError) as exc:
                raise BatteryQueryError(
                    f"打开 Aula A7 Pro 失败：{type(exc).__name__}: {exc}。"
                    "如厂商驱动中心正在占用该通道，请先退出后重试。"
                ) from exc
            self._dev = dev
        return self._dev

    def read_one(self, hid: Any, timeout_ms: int) -> BatteryStatus | None:
        dev = self._ensure_open(hid)
        if self._query_due():
            try:
                n = dev.write(QUERY)
                if n != len(QUERY):
                    raise BatteryQueryError(f"FF90 主动查询只写入 {n}/{len(QUERY)} 字节")
            except (OSError, ValueError, RuntimeError) as exc:
                raise BatteryQueryError(f"FF90 主动查询写入失败：{type(exc).__name__}: {exc}") from exc
        try:
            reply = dev.read(READ_LENGTH, timeout_ms=timeout_ms)
        except (OSError, ValueError, RuntimeError) as exc:
            # 常驻监听偶尔会在某一次读取里报瞬时错误。重开再读一次，否则本来
            # 只是设备暂时没上报，图标却要长时间刷成“读不到”。
            self.close()
            try:
                dev = self._ensure_open(hid)
                reply = dev.read(READ_LENGTH, timeout_ms=timeout_ms)
            except (OSError, ValueError, RuntimeError) as again:
                self.close()
                raise BatteryQueryError(
                    f"HID 读取失败：{type(again).__name__}: {again}"
                    f"（首次错误：{type(exc).__name__}）。接收器可能已被拔出。"
                ) from again
        return parse_report(bytes(reply)) if reply else None

    def probe(self, hid: Any) -> bool:
        try:
            find_target(hid)
        except BatteryQueryError:
            return False
        return True

    def close(self) -> None:
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:
                pass
            self._dev = None


def format_status(status: BatteryStatus, debug: bool = False) -> str:
    out = [
        f"[成功] {NAME} 电量读取完成",
        f"剩余电量：{status.percent}%",
        f"充电状态：{'正在充电' if status.wired_flag else '未充电'}",
        "连接方式：2.4GHz 接收器",
    ]
    if debug:
        out.append(f"原始上报：{status.raw.hex(' ').upper()}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"{NAME} (2.4GHz) 电量监听")
    parser.add_argument("--debug", action="store_true", help="显示原始上报报文")
    parser.add_argument("--seconds", type=int, default=30, help="最长监听秒数，默认 30")
    parser.add_argument("--active-query", action="store_true",
                        help="实验性：发送 64 字节查询请求，最短间隔 60 秒（需要自行确认风险）")
    args = parser.parse_args(argv)

    try:
        import hid  # supplied by the ``hidapi`` distribution
    except ImportError:
        print("未安装 hidapi。请运行：python -m pip install hidapi", file=sys.stderr)
        return 2

    print("=" * 56)
    print(f"{NAME} | 电量监听")
    print("目标：38A6:0001 / Usage Page FF90")
    print("=" * 56)

    if args.active_query:
        print("[实验性功能] 启用主动查询：每 60 秒最多写入一次 64 字节 HID Output Report。")
    driver = AulaA7Pro(active_poll=args.active_query)
    deadline = time.monotonic() + args.seconds
    try:
        while time.monotonic() < deadline:
            try:
                status = driver.read_one(hid, 1000)
            except BatteryQueryError as exc:
                print(f"[未成功] {exc}", file=sys.stderr)
                return 1
            if status is not None:
                print(format_status(status, debug=args.debug))
                return 0
            if args.debug:
                print("…等待耳机上报（可重新打开耳机或插拔充电线触发事件）", flush=True)
        print(f"[未成功] {args.seconds} 秒内没有收到有效电量报告。"
              "可重新开机、连接充电线或按需启用 --active-query 重试。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已停止监听。")
        return 130
    finally:
        driver.close()


if __name__ == "__main__":
    sys.exit(main())
