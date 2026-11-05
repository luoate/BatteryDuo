#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ATK A9 Ultimate + 狼蛛 A7 Pro 双托盘电量监控（Windows 11）。

右下角同时显示鼠标、耳机 SVG 和各自电量；不用切换设备。
ATK 按间隔查询；A7 Pro 在另一个线程监听 FF90/0x55 事件，
可通过右键菜单手动启用实验性 60 秒以上主动查询。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import sys
import threading
import time
import winreg
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import Qt, QLockFile, QRectF, QThread, QTimer, QStandardPaths, Signal
from PySide6.QtGui import QActionGroup, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel, QMenu, QProgressBar,
                               QSystemTrayIcon, QVBoxLayout, QWidget, QWidgetAction)

import atk_a9_battery as atk
import aula_a7pro_battery as aula
from battery_types import BatteryQueryError, BatteryStatus

# 沿用旧版配置路径、注册表键和单实例锁，升级后旧设置仍然有效。
APP_NAME = "ATK A9 Tray"
DISPLAY_NAME = "ATK + 狼蛛 双设备电量"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "ATK A9 Tray"
THEME_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
INTERVALS = (30, 60, 120, 300, 600)
THRESHOLDS = (5, 10, 15, 20, 25, 30)
ICON_SIZES = (16, 20, 24, 32, 48)
PUSH_STALE_S = 1800  # 耳机超过 30 分钟未有新消息，显示为旧数据（灰色）
DEFAULTS = {"interval_s": 60, "warn_percent": 20, "notify_low": True,
            "headset_active_poll": False}
DIGITS_LIGHT_TASKBAR = {"high": "#1E8E4E", "mid": "#A86200", "low": "#C6363C", "idle": "#525252"}
DIGITS_DARK_TASKBAR = {"high": "#4CC38A", "mid": "#F5A524", "low": "#F0555C", "idle": "#BDBDBD"}
TAG_COLORS = {"mouse": "#26B7F1", "headset": "#BD86FF"}
ASSET_NAMES = {"mouse": "鼠标滚轮.svg", "headset": "耳机.svg"}


def config_dir() -> Path:
    return Path(os.environ.get("APPDATA") or str(Path.home())) / APP_NAME


def fmt_interval(seconds: int) -> str:
    return f"{seconds // 60} 分钟" if seconds >= 60 and seconds % 60 == 0 else f"{seconds} 秒"


def theme_is_light() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, THEME_KEY) as key:
            return bool(winreg.QueryValueEx(key, "SystemUsesLightTheme")[0])
    except OSError:
        return False


def icon_sizes() -> tuple[int, ...]:
    try:
        physical = ctypes.windll.user32.GetSystemMetrics(49) or 16
    except (OSError, AttributeError):
        physical = 16
    return tuple(sorted(set(ICON_SIZES) | {physical, physical * 2}))


def digit_color(percent: int | None, stale: bool = False) -> QColor:
    palette = DIGITS_LIGHT_TASKBAR if theme_is_light() else DIGITS_DARK_TASKBAR
    if stale or percent is None:
        return QColor(palette["idle"])
    label = "low" if percent <= 10 else "mid" if percent <= 25 else "high"
    return QColor(palette[label])


@lru_cache(maxsize=128)
def device_pixmap(key: str, size: int, color: str) -> QPixmap:
    """从源码目录或 PyInstaller 资源目录加载 SVG，按主题着色。"""
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    renderer = QSvgRenderer(str(root / ASSET_NAMES[key]))
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    if renderer.isValid():
        renderer.render(p, QRectF(0, 0, size, size))
    else:
        # 素材被移动时仍保留可辨认的设备轮廓。
        p.setPen(QPen(Qt.white, max(1.0, size / 12)))
        if key == "mouse":
            p.drawRoundedRect(QRectF(size * .24, size * .08, size * .52, size * .84),
                              size * .24, size * .24)
            p.drawLine(int(size * .5), int(size * .22), int(size * .5), int(size * .42))
        else:
            p.drawArc(QRectF(size * .14, size * .1, size * .72, size * .72), 0, 180 * 16)
            for x in (.12, .7):
                p.drawRoundedRect(QRectF(size * x, size * .43, size * .18, size * .4), 1, 1)
    p.setCompositionMode(QPainter.CompositionMode_SourceIn)
    p.fillRect(pm.rect(), QColor(color))
    p.end()
    return pm


def paint_battery(size: int, percent: int | None, charging: bool,
                  stale: bool, key: str) -> QPixmap:
    """大设备轮廓 + 底部电量条，将有限的托盘空间留给图案。"""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    bar_h = max(2, round(size * .12))
    gap = max(1, round(size * .04))
    glyph_size = size - bar_h - gap
    color = digit_color(None, True).name() if stale or percent is None else TAG_COLORS[key]
    p.drawPixmap((size - glyph_size) // 2, 0, device_pixmap(key, glyph_size, color))

    margin = max(1, round(size * .06))
    bar = QRectF(margin, size - bar_h, size - 2 * margin, bar_h)
    track = QColor("#CFD6DF" if theme_is_light() else "#465365")
    p.setPen(Qt.NoPen)
    p.setBrush(track)
    p.drawRoundedRect(bar, bar_h / 2, bar_h / 2)
    if percent is not None and percent > 0:
        fill = QRectF(bar.x(), bar.y(), max(1, bar.width() * percent / 100), bar.height())
        p.setBrush(digit_color(percent, stale))
        p.drawRoundedRect(fill, bar_h / 2, bar_h / 2)
    if charging and not stale:
        # 蓝点表示耳机正在充电，不再额外挤占底部图标空间。
        dot = max(2, round(size * .14))
        p.setBrush(QColor("#4587FF"))
        p.drawEllipse(QRectF(size - dot, 0, dot, dot))
    p.end()
    return pm


def build_icon(percent: int | None, charging: bool, stale: bool, key: str) -> QIcon:
    icon = QIcon()
    for size in icon_sizes():
        icon.addPixmap(paint_battery(size, percent, charging, stale, key))
    return icon


class BatteryReader(QThread):
    """单个设备专属工作线程，不会因为耳机阻塞读取而卡住鼠标轮询。"""
    ok = Signal(object)
    err = Signal(str)

    def __init__(self, driver, interval_s: int, timeout_ms: int = 1200):
        super().__init__()
        self.driver = driver
        self.interval_s = interval_s
        self.timeout_ms = timeout_ms
        self._stop = threading.Event()
        self._wake = threading.Event()

    def refresh_now(self) -> None:
        if self.driver.push:
            self.driver.request_refresh()
        self._wake.set()

    def stop_reading(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:
        try:
            import hid
        except ImportError:
            self.err.emit("未安装 hidapi；请运行安装依赖.bat")
            return
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    status = self.driver.read_one(hid, self.timeout_ms)
                except BatteryQueryError as exc:
                    self.err.emit(str(exc))
                except Exception as exc:
                    self.err.emit(f"读取异常：{type(exc).__name__}: {exc}")
                else:
                    if status is not None:
                        self.ok.emit(status)
                if self.driver.push:
                    # 如果 HID 读取马上返回空，避免高速空轮询。
                    self._wake.wait(max(0.1, 0.15 - (time.monotonic() - started)))
                else:
                    self._wake.wait(max(0.0, self.interval_s - (time.monotonic() - started)))
                self._wake.clear()
        finally:
            self.driver.close()


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        raw = json.loads((config_dir() / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    for key, default in DEFAULTS.items():
        if type(raw.get(key)) is type(default):
            cfg[key] = raw[key]
    if cfg["interval_s"] not in INTERVALS:
        cfg["interval_s"] = DEFAULTS["interval_s"]
    if cfg["warn_percent"] not in THRESHOLDS:
        cfg["warn_percent"] = DEFAULTS["warn_percent"]
    return cfg


class DeviceTray:
    """一个设备持有一个图标、一份状态和一个独立 HID 读取线程。"""
    def __init__(self, owner: "TrayApp", key: str, driver):
        self.owner = owner
        self.key = key
        self.driver = driver
        self.status: BatteryStatus | None = None
        self.updated_at: float | None = None
        self.error: str | None = None
        self.cached = False
        self.warned_low = False
        self.reader = BatteryReader(driver, owner.cfg["interval_s"])
        self.reader.ok.connect(self.on_status)
        self.reader.err.connect(self.on_error)
        self.tray = QSystemTrayIcon(build_icon(None, False, False, key))
        self.tray.setToolTip(f"{driver.name}：等待电量…")
        self.tray.activated.connect(self.on_activated)
        self.actions: dict = {}

    @property
    def is_stale(self) -> bool:
        if self.cached or self.error:
            return True
        return bool(self.driver.push and self.updated_at and time.time() - self.updated_at > PUSH_STALE_S)

    def repaint(self) -> None:
        st = self.status
        self.tray.setIcon(build_icon(st.percent if st else None,
                                     st.wired_flag if st and self.key == "headset" else False,
                                     self.is_stale, self.key))
        # 悬浮在任意一个图标上，都能快速看到两种设备。
        self.tray.setToolTip(self.owner.tooltip())

    def on_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            # 点击设备图标刷新；耳机需已启用主动查询。
            self.reader.refresh_now()

    def on_status(self, status: BatteryStatus) -> None:
        self.status = status
        self.updated_at = time.time()
        self.error = None
        self.cached = False
        if self.key == "headset":
            self.owner.save_headset_cache(self)
        self.owner.log(f"{self.driver.name}: {status.percent}% charging={int(status.wired_flag)}")
        if self.owner.cfg["notify_low"] and status.percent <= self.owner.cfg["warn_percent"]:
            # 耳机正在充电时不提醒；鼠标的 wired_flag 是有线标志，不可靠地代表充电。
            if self.key != "headset" or not status.wired_flag:
                if not self.warned_low:
                    self.tray.showMessage(self.driver.name, f"剩余电量 {status.percent}%，请及时充电。",
                                          QSystemTrayIcon.Warning, 6000)
                self.warned_low = True
            else:
                self.warned_low = False
        elif status.percent > self.owner.cfg["warn_percent"] + 5:
            self.warned_low = False
        self.owner.update_icons_and_menus()

    def on_error(self, message: str) -> None:
        if self.error == message:
            return
        self.error = message
        self.owner.log(f"{self.driver.name}: {message}")
        # 保留上一次有效电量但灰显，绝不将离线的接收器当成实时 0%。
        self.owner.update_icons_and_menus()

    def summary(self) -> str:
        short = "鼠标" if self.key == "mouse" else "耳机"
        if self.status is None:
            return f"{short}：--%" + ("（读取失败）" if self.error else "（等待数据）")
        parts = [f"{short}：{self.status.percent}%"]
        if self.key == "headset":
            parts.append("充电中" if self.status.wired_flag else "未充电")
        if self.is_stale:
            parts.append("上次数据")
        return " · ".join(parts)

    def detail(self) -> str:
        if not self.updated_at:
            return ("等待耳机电量（主动查询已开启）" if self.driver.push and self.owner.cfg["headset_active_poll"]
                    else "等待耳机主动上报（主动查询未开启）" if self.driver.push
                    else "鼠标按设置间隔查询")
        updated = datetime.fromtimestamp(self.updated_at).strftime("%H:%M:%S")
        if self.is_stale:
            note = "（历史数据，不保证是当前电量）"
        else:
            note = ""
        if self.error:
            note += "｜读取异常：" + self.error[:50]
        if self.driver.push:
            age = int(max(0, time.time() - self.updated_at) // 60)
            return f"耳机更新于 {updated}（{age} 分钟前）{note}"
        volt = f"｜{self.status.voltage_mv} mV" if self.status and self.status.voltage_mv else ""
        return f"鼠标更新于 {updated}{volt}{note}"


class DeviceStatusCard(QWidget):
    """菜单中的只读设备卡片，保留历史/异常信息，不占用菜单操作项。"""
    def __init__(self, parent: QMenu):
        super().__init__(parent)
        self.setObjectName("deviceCard")
        self.setFixedWidth(332)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.glyph = QLabel()
        self.glyph.setFixedSize(28, 28)
        self.title = QLabel()
        self.title.setObjectName("deviceTitle")
        self.value = QLabel()
        self.value.setObjectName("batteryValue")
        row.addWidget(self.glyph)
        row.addWidget(self.title, 1)
        row.addWidget(self.value)
        layout.addLayout(row)
        self.state = QLabel()
        self.detail = QLabel()
        self.detail.setObjectName("deviceDetail")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedHeight(4)
        layout.addWidget(self.state)
        layout.addWidget(self.bar)
        layout.addWidget(self.detail)

    def sync(self, device: DeviceTray) -> None:
        light = theme_is_light()
        fg, muted = ("#17212F", "#64748B") if light else ("#EDF2F7", "#9CAABD")
        bg, track = ("#F1F5F9", "#DCE4ED") if light else ("#263141", "#3A4759")
        st = device.status
        color = digit_color(st.percent if st else None, device.is_stale).name()
        accent = muted if device.is_stale or st is None else TAG_COLORS[device.key]
        self.setStyleSheet(f"""
            QWidget#deviceCard {{ background: {bg}; border-radius: 8px; }}
            QLabel {{ color: {fg}; background: transparent; font: 9pt 'Segoe UI'; }}
            QLabel#deviceTitle {{ font-weight: 600; }}
            QLabel#batteryValue {{ color: {color}; font: bold 18pt 'Segoe UI'; }}
            QLabel#deviceDetail {{ color: {muted}; font-size: 8pt; }}
            QProgressBar {{ border: none; border-radius: 2px; background: {track}; }}
            QProgressBar::chunk {{ border-radius: 2px; background: {color}; }}
        """)
        dpr = self.devicePixelRatioF()
        pm = QPixmap(device_pixmap(device.key, round(28 * dpr), accent))
        pm.setDevicePixelRatio(dpr)
        self.glyph.setPixmap(pm)
        self.title.setText("ATK A9 Ultimate" if device.key == "mouse" else "狼蛛 A7 Pro")
        self.value.setText(f"{st.percent}%" if st else "--%")
        if st is None:
            state = "读取失败 · 等待有效数据" if device.error else "等待设备电量"
        elif device.is_stale:
            state = "历史电量 · 等待更新"
        elif device.key == "headset":
            state = "正在充电" if st.wired_flag else "电量已更新 · 未充电"
        else:
            state = "电量已更新"
        self.state.setText(state)
        self.bar.setValue(st.percent if st else 0)
        detail = device.detail()
        if device.error and not device.updated_at:
            detail = "读取异常：" + device.error
        self.detail.setText(self.detail.fontMetrics().elidedText(detail, Qt.ElideRight, 304))
        self.setToolTip(device.summary() + "\n" + detail)


class TrayApp:
    def __init__(self, app: QApplication, cfg: dict, debug: bool = False):
        self.app = app
        self.cfg = cfg
        self.debug = debug
        self.log_path = config_dir() / "tray.log"
        self.mouse = DeviceTray(self, "mouse", atk.AtkA9Ultimate())
        self.headset = DeviceTray(self, "headset", aula.AulaA7Pro(
            active_poll=cfg["headset_active_poll"], interval_s=cfg["interval_s"]))
        self.devices = (self.mouse, self.headset)
        self._restore_headset_cache()
        for device in self.devices:
            menu = self._build_menu(device)
            device.menu = menu  # 菜单必须存活到整个程序退出
            device.tray.setContextMenu(menu)
        self.stale_timer = QTimer()
        self.stale_timer.timeout.connect(self.update_icons_and_menus)
        self.stale_timer.start(60000)
        self.update_icons_and_menus()

    def log(self, text: str) -> None:
        if not self.debug:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fp:
                fp.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
        except OSError:
            pass

    def _save_config(self) -> None:
        try:
            config_dir().mkdir(parents=True, exist_ok=True)
            (config_dir() / "config.json").write_text(
                json.dumps(self.cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            self.log(f"保存配置失败：{exc}")

    def save_headset_cache(self, device: DeviceTray) -> None:
        """只缓存最近收到的耳机事件；下次启动仍作为历史数据灰显。"""
        if not device.status or device.updated_at is None:
            return
        try:
            config_dir().mkdir(parents=True, exist_ok=True)
            (config_dir() / "headset_last.json").write_text(json.dumps({
                "percent": device.status.percent, "charging": device.status.wired_flag,
                "updated_at": device.updated_at
            }), encoding="utf-8")
        except OSError as exc:
            self.log(f"缓存耳机电量失败：{exc}")

    def _restore_headset_cache(self) -> None:
        try:
            data = json.loads((config_dir() / "headset_last.json").read_text(encoding="utf-8"))
            value, ts, charging = data["percent"], data["updated_at"], data["charging"]
            if type(value) is int and 0 <= value <= 100 and type(charging) is bool and \
                    isinstance(ts, (float, int)) and 0 <= time.time() - ts < 7 * 86400:
                self.headset.status = BatteryStatus(value, None, charging, b"")
                self.headset.updated_at = ts
                self.headset.cached = True
        except (OSError, ValueError, TypeError, KeyError):
            pass

    def tooltip(self) -> str:
        return "ATK + 狼蛛电量\n" + "\n".join(d.summary() for d in self.devices)

    def _build_menu(self, device: DeviceTray) -> QMenu:
        menu = QMenu()
        acts = device.actions
        for key in ("mouse", "headset"):
            card = DeviceStatusCard(menu)
            action = QWidgetAction(menu)
            action.setDefaultWidget(card)
            menu.addAction(action)
            acts[key + "_card"] = card
        menu.addSeparator()
        menu.addAction("立即查询鼠标电量").triggered.connect(self.mouse.reader.refresh_now)
        acts["headset_query"] = menu.addAction("立即查询耳机电量")
        acts["headset_query"].triggered.connect(self.headset.reader.refresh_now)
        settings = menu.addMenu("设置")
        acts["active_query"] = settings.addAction("耳机主动查询（实验性，间隔≥60秒）")
        acts["active_query"].setCheckable(True)
        acts["active_query"].toggled.connect(self._set_headset_active_poll)
        acts["notify"] = settings.addAction("低电量弹窗提醒")
        acts["notify"].setCheckable(True)
        acts["notify"].toggled.connect(self._set_notify)

        submenu = settings.addMenu("低电量提醒阈值")
        group = QActionGroup(submenu)
        group.setExclusive(True)
        acts["warn_group"] = group
        acts["warn_choices"] = {}
        for percent in THRESHOLDS:
            act = submenu.addAction(f"≤ {percent}%")
            act.setCheckable(True)
            act.setData(percent)
            group.addAction(act)
            act.triggered.connect(lambda checked=False, n=percent: self._set_warn(n))
            acts["warn_choices"][percent] = act

        submenu = settings.addMenu("刷新间隔")
        group = QActionGroup(submenu)
        group.setExclusive(True)
        acts["interval_group"] = group
        acts["interval_choices"] = {}
        for seconds in INTERVALS:
            act = submenu.addAction(fmt_interval(seconds))
            act.setCheckable(True)
            act.setData(seconds)
            group.addAction(act)
            act.triggered.connect(lambda checked=False, n=seconds: self._set_interval(n))
            acts["interval_choices"][seconds] = act

        acts["autostart"] = settings.addAction("登录时自动启动")
        acts["autostart"].setCheckable(True)
        acts["autostart"].toggled.connect(self._set_autostart)
        menu.addSeparator()
        menu.addAction("退出双设备监控").triggered.connect(self.app.quit)
        menu.aboutToShow.connect(self._sync_menus)
        return menu

    def _sync_menus(self) -> None:
        light = theme_is_light()
        bg, fg, hover, border = (("#FFFFFF", "#17212F", "#E7F1FC", "#DCE4ED") if light
                                 else ("#1B2431", "#EDF2F7", "#30445D", "#354154"))
        style = f"""
            QMenu {{ background: {bg}; color: {fg}; border: 1px solid {border};
                     padding: 6px; font: 10pt 'Segoe UI'; }}
            QMenu::item {{ padding: 8px 28px; border-radius: 4px; }}
            QMenu::item:selected {{ background: {hover}; }}
            QMenu::item:disabled {{ color: #8491A3; }}
            QMenu::separator {{ height: 1px; background: {border}; margin: 6px 8px; }}
        """
        for device in self.devices:
            a = device.actions
            device.menu.setStyleSheet(style)
            a["mouse_card"].sync(self.mouse)
            a["headset_card"].sync(self.headset)
            a["headset_query"].setEnabled(self.cfg["headset_active_poll"])
            for name, value in (("notify", self.cfg["notify_low"]),
                                ("active_query", self.cfg["headset_active_poll"]),
                                ("autostart", autostart_enabled())):
                act = a[name]
                act.blockSignals(True)
                act.setChecked(value)
                act.blockSignals(False)
            for v, act in a["warn_choices"].items():
                act.setChecked(v == self.cfg["warn_percent"])
            for v, act in a["interval_choices"].items():
                act.setChecked(v == self.cfg["interval_s"])

    def update_icons_and_menus(self) -> None:
        for d in self.devices:
            d.repaint()
        self._sync_menus()

    def _set_notify(self, on: bool) -> None:
        self.cfg["notify_low"] = bool(on)
        self._save_config()
        self._sync_menus()

    def _set_warn(self, percent: int) -> None:
        self.cfg["warn_percent"] = percent
        for d in self.devices:
            d.warned_low = False
        self._save_config()
        self._sync_menus()

    def _set_interval(self, seconds: int) -> None:
        self.cfg["interval_s"] = seconds
        self.mouse.reader.interval_s = seconds
        # 耳机的主动查询最短为 60 秒，即使用户把鼠标设置为 30 秒。
        self.headset.driver.set_interval(seconds)
        self.mouse.reader.refresh_now()
        self._save_config()
        self._sync_menus()

    def _set_headset_active_poll(self, enabled: bool) -> None:
        self.cfg["headset_active_poll"] = bool(enabled)
        self.headset.driver.set_active_poll(enabled)
        if enabled:
            self.headset.reader.refresh_now()
        self._save_config()
        self._sync_menus()

    def _set_autostart(self, enabled: bool) -> None:
        try:
            set_autostart(enabled)
        except OSError as exc:
            self.mouse.tray.showMessage(DISPLAY_NAME, f"设置开机启动失败：{exc}",
                                        QSystemTrayIcon.Warning, 5000)
        self._sync_menus()

    def start(self) -> None:
        # 两个图标始终显示；即使只有一个接收器插着也不隐藏另外一个。
        for d in self.devices:
            d.tray.show()
            d.reader.start()

    def shutdown(self) -> None:
        self.stale_timer.stop()
        for d in self.devices:
            d.reader.stop_reading()
        for d in self.devices:
            d.reader.wait(5000)
            d.tray.hide()


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return bool(winreg.QueryValueEx(key, RUN_VALUE)[0])
    except OSError:
        return False


def launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    interpreter = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{interpreter}" "{Path(__file__).resolve()}"'


def set_autostart(enable: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enable:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


def set_app_user_model_id() -> None:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ATK.AULA.DualBattery.1")
    except (OSError, AttributeError):
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ATK A9 Ultimate + 狼蛛 A7 Pro 双托盘电量监控")
    parser.add_argument("--interval", type=int, choices=INTERVALS)
    parser.add_argument("--warn", type=int, choices=THRESHOLDS)
    parser.add_argument("--debug", action="store_true", help=f"记录日志到 {config_dir() / 'tray.log'}")
    parser.add_argument("--self-test", type=Path, metavar="REPORT",
                        help="离线验证打包后的 Qt、SVG 和菜单，将结果写入 REPORT；不访问 HID")
    args = parser.parse_args(argv)
    app = QApplication(sys.argv)
    app.setApplicationName(DISPLAY_NAME)
    app.setQuitOnLastWindowClosed(False)
    if args.self_test:
        # 在单实例锁和 HID 初始化之前验证，测试不启动任何读取线程。
        for key, asset in ASSET_NAMES.items():
            resource = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / asset
            if not QSvgRenderer(str(resource)).isValid():
                raise RuntimeError(f"SVG resource is invalid: {asset}")
            for percent in (None, 0, 5, 20, 86, 100):
                for stale in (False, True):
                    icon = build_icon(percent, True, stale, key)
                    if icon.pixmap(16, 16).isNull():
                        raise RuntimeError(f"Tray icon render failed: {key}")
        monitor = TrayApp(app, dict(DEFAULTS))
        try:
            for device in monitor.devices:
                device.menu.ensurePolished()
                device.menu.resize(device.menu.sizeHint())
                if device.menu.grab().isNull():
                    raise RuntimeError("Menu render failed")
        finally:
            monitor.shutdown()
        args.self_test.write_text(json.dumps({"ok": True, "qt": "loaded",
                                             "svg": "rendered", "menus": "rendered",
                                             "hid_accessed": False}), encoding="utf-8")
        return 0
    set_app_user_model_id()
    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("Windows 系统托盘不可用。", file=sys.stderr)
        return 2
    lock_dir = Path(QStandardPaths.writableLocation(QStandardPaths.TempLocation))
    lock = QLockFile(str(lock_dir / "atk-a9-tray.lock"))  # 与 battery.zip 旧版共用单实例锁
    lock.setStaleLockTime(5000)
    if not lock.tryLock(100):
        print("托盘程序已在运行。请先从右下角退出旧版，再重新启动。", file=sys.stderr)
        return 1
    try:
        try:
            import hid  # noqa: F401
        except ImportError:
            print("缺少 hidapi：请先运行 安装依赖.bat", file=sys.stderr)
            return 2
        cfg = load_config()
        if args.interval is not None:
            cfg["interval_s"] = args.interval
        if args.warn is not None:
            cfg["warn_percent"] = args.warn
        monitor = TrayApp(app, cfg, debug=args.debug)
        monitor.start()
        try:
            return app.exec()
        finally:
            monitor.shutdown()
    finally:
        lock.unlock()


if __name__ == "__main__":
    sys.exit(main())
