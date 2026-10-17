#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATK A9 Ultimate (2.4 GHz) battery query for Windows 11.

Supported verified-on-user-descriptors interface: VID:PID 373B:11D9,
Usage Page FF02, Usage 0002, Interface 1. Does not modify settings.

Requires ``hidapi`` (pip install hidapi). Query format adapted from
Fan4Metal/mouse_tray/drivers/chipset/nordic52.py (public reference).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from battery_types import BatteryQueryError, BatteryStatus

VID = 0x373B
PID = 0x11D9
USAGE_PAGE = 0xFF02
USAGE = 0x0002
INTERFACE = 1
REPORT_ID = 0x08
READ_LENGTH = 17
# The fixed, known battery read request; last byte makes sum(request) % 256 == 0x55
QUERY = bytes([0x08, 0x04] + [0x00] * 14 + [0x49])


def status_to_json(status: BatteryStatus) -> dict[str, Any]:
    return {
        "device": "ATK A9 Ultimate",
        "connection": "2.4GHz receiver",
        "percent": status.percent,
        "voltage_mv": status.voltage_mv,
        "wired_flag": status.wired_flag,
    }


def find_target(hid: Any) -> dict[str, Any]:
    """Return exactly the target collection. Do not open a generic mouse interface."""
    try:
        devices = hid.enumerate(VID, PID)
    except Exception as exc:
        raise BatteryQueryError(f"枚举 HID 接收器失败：{exc}") from exc
    matches = [
        d for d in devices
        if d.get("vendor_id") == VID
        and d.get("product_id") == PID
        and d.get("usage_page") == USAGE_PAGE
        and d.get("usage") == USAGE
        and d.get("interface_number") == INTERFACE
        and d.get("path")
    ]
    if not matches:
        raise BatteryQueryError(
            "未找到目标接口 373B:11D9 / FF02:0002 / Interface 1。"
            "请确认 2.4GHz 接收器仍已插入，并先运行第一阶段检测工具。"
        )
    return matches[0]


def parse_reply(reply: Any) -> BatteryStatus:
    """Validate reply identity, length and checksum before interpreting percent."""
    if not reply:
        raise BatteryQueryError("未收到响应（鼠标可能休眠、关闭或暂时与接收器失联）。")
    try:
        data = bytes(reply)
    except (TypeError, ValueError) as exc:
        raise BatteryQueryError(f"返回的数据类型无法解析：{exc}") from exc

    raw_hex = data.hex(" ").upper()
    if len(data) != READ_LENGTH:
        raise BatteryQueryError(
            f"响应长度异常：收到 {len(data)} 字节，预期 {READ_LENGTH} 字节。"
            f" 原始响应：{raw_hex}"
        )
    if data[0] != REPORT_ID or data[1] != 0x04:
        raise BatteryQueryError(
            "响应不是预期的电量查询报文（前两字节应为 08 04）。"
            f" 原始响应：{raw_hex}"
        )
    if sum(data) % 256 != 0x55:
        raise BatteryQueryError(
            "电量响应校验和不匹配，为避免显示错误电量，本次没有解析。"
            f" 原始响应：{raw_hex}"
        )
    percent = data[6]
    if percent > 100:
        raise BatteryQueryError(
            f"电量字段超出 0–100 范围（读到 {percent}）。"
            f" 原始响应：{raw_hex}"
        )
    # Report byte 8:9 represents a big-endian voltage reading in millivolts.
    mv = (data[8] << 8) | data[9]
    voltage_mv = mv if 2500 <= mv <= 5000 else None
    return BatteryStatus(percent, voltage_mv, bool(data[7]), data)


def query_once(hid: Any, timeout_ms: int) -> BatteryStatus:
    desc = find_target(hid)
    dev = None
    try:
        dev = hid.device()
        # Exactly the matched FF02:0002 HID collection; never use open(VID, PID).
        dev.open_path(desc["path"])
        count = dev.write(list(QUERY))
        # Some wrappers may return None for successful writes; negative/short means failure.
        if count is not None and count != len(QUERY):
            raise BatteryQueryError(
                f"发送查询报文不完整：写入 {count} 字节，预期 {len(QUERY)} 字节。"
            )
        time.sleep(0.1)  # Source implementation allows device to prepare a reply.
        reply = dev.read(READ_LENGTH, timeout_ms=timeout_ms)
        return parse_reply(reply)
    except BatteryQueryError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise BatteryQueryError(
            f"HID 通信失败：{type(exc).__name__}: {exc}。"
            "如 ATK HUB 正在访问接收器，请先退出 HUB 并重试。"
        ) from exc
    finally:
        if dev is not None:
            try:
                dev.close()
            except Exception:
                pass


class AtkA9Ultimate:
    """查询式驱动，供托盘复用：每次开-发-读-关。"""

    name = "ATK A9 Ultimate"
    push = False
    charge_label = "检测到有线标志"

    def read_one(self, hid: Any, timeout_ms: int) -> BatteryStatus:
        return query_once(hid, timeout_ms)

    def probe(self, hid: Any) -> bool:
        try:
            find_target(hid)
        except BatteryQueryError:
            return False
        return True

    def close(self) -> None:
        # 查询式驱动用完即关，没有常驻句柄需要释放。
        pass


def format_status(status: BatteryStatus, debug: bool = False) -> str:
    out = [
        "[成功] ATK A9 Ultimate 电量读取完成",
        f"剩余电量：{status.percent}%",
        "连接方式：2.4GHz 接收器",
    ]
    if status.voltage_mv is not None:
        out.append(f"电池电压：{status.voltage_mv} mV（协议提供的参考值）")
    else:
        out.append("电池电压：无有效电压值（不影响电量百分比）")
    out.append(f"有线标志：{'1' if status.wired_flag else '0'}（无线查询一般为 0；不能单凭它判断正在充电）")
    if debug:
        out.append(f"请求报文：{QUERY.hex(' ').upper()}")
        out.append(f"原始响应：{status.raw.hex(' ').upper()}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ATK A9 Ultimate (2.4GHz) 电量读取 - 阶段 2")
    parser.add_argument("--debug", action="store_true", help="显示原始 HID 查询和响应，用于排错")
    parser.add_argument("--json", action="store_true", help="只输出机器可读的 JSON 结果")
    parser.add_argument("--watch", type=int, metavar="秒", help="持续读取，刷新间隔至少 5 秒（推荐 60 秒）")
    parser.add_argument("--timeout", type=int, default=1000, metavar="毫秒", help="读取超时：100–5000 毫秒，默认 1000")
    args = parser.parse_args(argv)

    if not 100 <= args.timeout <= 5000:
        parser.error("--timeout 必须在 100–5000 毫秒之间")
    if args.watch is not None and not 5 <= args.watch <= 86400:
        parser.error("--watch 必须在 5–86400 秒之间")
    if args.watch is not None and args.json:
        parser.error("--json 仅支持单次读取；持续输出请使用 --watch（不加 --json）")

    try:
        import hid  # supplied by the ``hidapi`` distribution
    except ImportError:
        print("未安装 hidapi。请运行：python -m pip install hidapi", file=sys.stderr)
        return 2

    if not args.json:
        print("=" * 56)
        print("ATK A9 Ultimate | 电量读取（阶段 2）")
        print("目标：373B:11D9 / FF02:0002 / Interface 1")
        print("=" * 56)

    while True:
        try:
            status = query_once(hid, args.timeout)
            if args.json:
                print(json.dumps({"ok": True, **status_to_json(status)}, ensure_ascii=False))
            else:
                print(format_status(status, debug=args.debug))
            exit_code = 0
        except BatteryQueryError as exc:
            if args.json:
                print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            else:
                print(f"[未成功] {exc}", file=sys.stderr)
                print("建议：唤醒鼠标、检查接收器、退出 ATK HUB 后重试；仍有问题可运行 --debug。", file=sys.stderr)
            exit_code = 1
        except KeyboardInterrupt:
            if not args.json:
                print("\n已停止读取。")
            return 130

        if args.watch is None:
            return exit_code
        try:
            print(f"\n（{args.watch} 秒后刷新，按 Ctrl+C 停止）", flush=True)
            time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\n已停止读取。")
            return 0


if __name__ == "__main__":
    sys.exit(main())
