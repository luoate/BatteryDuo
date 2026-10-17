"""Offline unit tests for protocol parser and device selection (no physical HID)."""
import unittest
from unittest.mock import Mock, patch
import atk_a9_battery as a
import aula_a7pro_battery as u


class ProtocolTests(unittest.TestCase):
    def test_request_has_17_bytes_and_checksum(self):
        self.assertEqual(len(a.QUERY), 17)
        self.assertEqual(a.QUERY[:2], b'\x08\x04')
        self.assertEqual(sum(a.QUERY) % 256, 0x55)

    def test_known_response(self):
        # Reference packet given by mouse_tray for 35%, 3732 mV.
        reply = bytes.fromhex("08 04 00 00 00 02 23 00 0e 94 00 00 00 00 00 00 82")
        status = a.parse_reply(reply)
        self.assertEqual(status.percent, 35)
        self.assertEqual(status.voltage_mv, 3732)
        self.assertFalse(status.wired_flag)

    def test_empty_response(self):
        with self.assertRaises(a.BatteryQueryError):
            a.parse_reply([])

    def test_bad_checksum(self):
        reply = bytearray.fromhex("08 04 00 00 00 02 23 00 0e 94 00 00 00 00 00 00 82")
        reply[16] ^= 0x01
        with self.assertRaisesRegex(a.BatteryQueryError, "校验和"):
            a.parse_reply(reply)

    def test_bad_prefix(self):
        reply = bytearray.fromhex("08 04 00 00 00 02 23 00 0e 94 00 00 00 00 00 00 82")
        reply[1] = 5
        with self.assertRaisesRegex(a.BatteryQueryError, "不是预期"):
            a.parse_reply(reply)

    def test_interface_exact_match(self):
        hid = Mock()
        hid.enumerate.return_value = [
            {"vendor_id": 0x373B, "product_id": 0x11D9, "usage_page": 0x0001,
             "usage": 2, "interface_number": 1, "path": b"other"},
            {"vendor_id": 0x373B, "product_id": 0x11D9, "usage_page": 0xFF02,
             "usage": 2, "interface_number": 1, "path": b"target"},
        ]
        self.assertEqual(a.find_target(hid)["path"], b"target")

    def test_transaction_mock(self):
        reply = bytes.fromhex("08 04 00 00 00 02 23 00 0e 94 00 00 00 00 00 00 82")
        device = Mock()
        device.write.return_value = 17
        device.read.return_value = list(reply)
        hid = Mock()
        hid.device.return_value = device
        hid.enumerate.return_value = [{"vendor_id": a.VID, "product_id": a.PID,
                                        "usage_page": a.USAGE_PAGE, "usage": a.USAGE,
                                        "interface_number": a.INTERFACE, "path": b"target"}]
        status = a.query_once(hid, timeout_ms=1000)
        self.assertEqual(status.percent, 35)
        device.open_path.assert_called_once_with(b"target")
        device.write.assert_called_once_with(list(a.QUERY))
        device.read.assert_called_once_with(17, timeout_ms=1000)
        device.close.assert_called_once()


class AulaProtocolTests(unittest.TestCase):
    REPORT = bytes.fromhex("55 65 50 03 00 00 00 00 00 00 00 00")

    def test_captured_report(self):
        # Real report captured from the device: 0x50 = 80 %, 0x03 = charging.
        st = u.parse_report(self.REPORT)
        self.assertEqual(st.percent, 80)
        self.assertTrue(st.wired_flag)
        self.assertIsNone(st.voltage_mv)

    def test_not_charging(self):
        st = u.parse_report(bytes.fromhex("55 65 3c 02 00 00 00 00 00 00 00 00"))
        self.assertEqual(st.percent, 60)
        self.assertFalse(st.wired_flag)

    def test_other_reports_are_ignored(self):
        self.assertIsNone(u.parse_report(bytes.fromhex("55 07 50 03 00 00")))
        self.assertIsNone(u.parse_report(bytes.fromhex("08 04 00 00 00 00")))
        self.assertIsNone(u.parse_report(b"\x55\x65"))

    def test_out_of_range_percent(self):
        with self.assertRaises(u.BatteryQueryError):
            u.parse_report(bytes.fromhex("55 65 65 03 00 00"))

    def test_power_off_event_not_reported_as_zero_battery(self):
        # Actual captured disconnect message: 55 65 00 1A.
        with self.assertRaisesRegex(u.BatteryQueryError, "关闭|断开"):
            u.parse_report(bytes.fromhex("55 65 00 1A 00 00 00 00"))

    def test_real_zero_percent_still_valid_when_mode_is_02(self):
        # 0% is only valid in a normal charging-state report, not an off event.
        st = u.parse_report(bytes.fromhex("55 65 00 02 00 00 00 00"))
        self.assertEqual(st.percent, 0)
        self.assertFalse(st.wired_flag)

    def test_unknown_charge_mode(self):
        with self.assertRaisesRegex(u.BatteryQueryError, "充电状态"):
            u.parse_report(bytes.fromhex("55 65 50 09 00 00"))

    def _listener_hid(self):
        hid = Mock()
        hid.enumerate.return_value = [
            {"vendor_id": u.VID, "product_id": u.PID, "usage_page": u.USAGE_PAGE,
             "usage": 1, "interface_number": 0, "path": b"target"}]
        return hid

    def test_transient_read_error_reopens_then_recovers(self):
        # 实测收到过一次瞬时 OSError；重开重读必须静默恢复，不能把图标刷成失联。
        hid = self._listener_hid()
        hid.device.return_value.read.side_effect = [OSError("read error"), list(self.REPORT)]
        drv = u.AulaA7Pro()
        self.assertEqual(drv.read_one(hid, 1000).percent, 80)
        self.assertEqual(hid.device.return_value.open_path.call_count, 2)
        self.assertEqual(hid.device.return_value.close.call_count, 1)

    def test_persistent_read_error_reports_unplug(self):
        hid = self._listener_hid()
        hid.device.return_value.read.side_effect = OSError("read error")
        drv = u.AulaA7Pro()
        with self.assertRaisesRegex(u.BatteryQueryError, "拔出"):
            drv.read_one(hid, 1000)
        self.assertIsNone(drv._dev)
        self.assertEqual(hid.device.return_value.close.call_count, 2)

    def test_ff90_collection_selected(self):
        hid = Mock()
        hid.enumerate.return_value = [
            {"vendor_id": u.VID, "product_id": u.PID, "usage_page": 0xFF82,
             "usage": 1, "interface_number": 0, "path": b"other"},
            {"vendor_id": u.VID, "product_id": u.PID, "usage_page": 0xFF90,
             "usage": 1, "interface_number": 0, "path": b"target"},
        ]
        self.assertEqual(u.find_target(hid)["path"], b"target")

    def test_active_query_packet(self):
        self.assertEqual(len(u.QUERY), 64)
        self.assertEqual(u.QUERY[:4], bytes.fromhex("55 65 01 00"))
        self.assertEqual(u.QUERY[4:], bytes(60))

    def test_active_query_default_disabled(self):
        dev = Mock()
        dev.read.return_value = list(self.REPORT)
        hid = self._listener_hid()
        hid.device.return_value = dev
        driver = u.AulaA7Pro()
        try:
            self.assertEqual(driver.read_one(hid, 300).percent, 80)
            dev.write.assert_not_called()
        finally:
            driver.close()

    def test_active_query_write_then_response(self):
        dev = Mock()
        dev.write.return_value = 64
        dev.read.return_value = list(bytes.fromhex("55 65 50 02") + bytes(60))
        hid = self._listener_hid()
        hid.device.return_value = dev
        driver = u.AulaA7Pro(active_poll=True)
        try:
            result = driver.read_one(hid, 1200)
            self.assertEqual(result.percent, 80)
            self.assertFalse(result.wired_flag)
            dev.write.assert_called_once_with(u.QUERY)
        finally:
            driver.close()

    def test_active_query_has_rate_limit_and_manual_gap(self):
        dev = Mock()
        dev.write.return_value = 64
        dev.read.return_value = []
        hid = self._listener_hid()
        hid.device.return_value = dev
        driver = u.AulaA7Pro(active_poll=True, interval_s=30)
        try:
            with patch.object(u.time, "monotonic", side_effect=[0, 5, 20, 59, 60]):
                driver.read_one(hid, 100)
                driver.read_one(hid, 100)
                driver.request_refresh()
                driver.read_one(hid, 100)
                driver.read_one(hid, 100)
                driver.read_one(hid, 100)
            # First write at 0, manual refresh at 20, normal poll at 60 (>=60 since manual).
            self.assertEqual(dev.write.call_count, 2)
        finally:
            driver.close()

    def test_active_query_may_be_disabled(self):
        dev = Mock()
        dev.write.return_value = 64
        dev.read.return_value = []
        hid = self._listener_hid()
        hid.device.return_value = dev
        driver = u.AulaA7Pro(active_poll=True)
        try:
            driver.set_active_poll(False)
            driver.read_one(hid, 100)
            dev.write.assert_not_called()
        finally:
            driver.close()

    def test_short_write_fails_instead_of_reporting_fake_battery(self):
        dev = Mock()
        dev.write.return_value = 12
        hid = self._listener_hid()
        hid.device.return_value = dev
        driver = u.AulaA7Pro(active_poll=True)
        try:
            with self.assertRaisesRegex(u.BatteryQueryError, "只写入"):
                driver.read_one(hid, 100)
        finally:
            driver.close()

    def test_listener_returns_none_on_timeout(self):
        device = Mock()
        device.read.side_effect = [list(self.REPORT), []]
        hid = Mock()
        hid.device.return_value = device
        hid.enumerate.return_value = [
            {"vendor_id": u.VID, "product_id": u.PID, "usage_page": 0xFF90,
             "usage": 1, "interface_number": 0, "path": b"target"}]
        drv = u.AulaA7Pro()
        self.assertEqual(drv.read_one(hid, 1000).percent, 80)
        self.assertIsNone(drv.read_one(hid, 1000))
        device.open_path.assert_called_once_with(b"target")
        device.close.assert_not_called()


if __name__ == '__main__':
    unittest.main()
