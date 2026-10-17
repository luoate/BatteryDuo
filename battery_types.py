#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两只鼠标驱动共用的数据结构和异常。"""

from __future__ import annotations

from dataclasses import dataclass


class BatteryQueryError(Exception):
    """预期内的读取失败，用于给用户展示中文原因。"""


@dataclass(frozen=True)
class BatteryStatus:
    percent: int
    voltage_mv: int | None
    wired_flag: bool
    raw: bytes
