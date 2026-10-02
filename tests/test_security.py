#!/usr/bin/env python3
"""Exercise start-ap security and config handling without Android hardware."""

from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import unittest


BACKEND = Path(__file__).resolve().parents[1] / "backend" / "start-ap"


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "virtualap"
        self.shared = Path(self.temp.name) / "ap.conf"
        self.base.joinpath("bin").mkdir(parents=True)
        # Use host applets for the fixture; production still uses bundled busybox.
        wrapper = self.base / "bin" / "busybox"
        wrapper.write_text('#!/bin/sh\ncase "$1" in\nid) echo 0 ;;\n*) exec "$@" ;;\nesac\n')
        wrapper.chmod(0o755)
        self.source = BACKEND.read_text().replace(
            'BASE_DIR="/data/local/virtualap"', f'BASE_DIR={shlex.quote(str(self.base))}'
        ).replace('/data/local/ap.conf', str(self.shared))
        self.definitions, self.main = self.source.split("# --- Main ---", 1)

    def shell(self, body, *, cli=None, check=True):
        script = self.definitions + '\nreg_country() { printf "US\\n"; }\n' + body
        if cli is not None:
            script += "\n" + self.main
        result = subprocess.run(
            ["sh", "-c", script, "start-ap", *(cli or [])],
            text=True, capture_output=True, check=check
        )
        return result.stdout

    def config(self, mode, pmf="1", bridge="", band="2", width="20"):
        self.shell(
            f"SECURITY={shlex.quote(mode)}; PMF={pmf}; PASSWORD=abcdefgh; "
            f"SSID=test; CHANNEL=36; BAND={band}; EFFECTIVE_WIDTH={width}; "
            f"write_hostapd_conf {shlex.quote(bridge)}"
        )
        return dict(
            line.split("=", 1) for line in (self.base / "run" / "hostapd.conf").read_text().splitlines()
            if "=" in line
        )

    def test_compatibility_ciphers_and_pmf(self):
        for band, width, bridge in (("2", "20", ""), ("5", "20", ""),
                                    ("5", "40", ""), ("5", "80", "vap-br0")):
            with self.subTest(band=band, width=width, bridge=bridge):
                conf = self.config("wpawpa2", bridge=bridge, band=band, width=width)
                self.assertEqual(conf["wpa"], "3")
                self.assertEqual(conf["wpa_key_mgmt"], "WPA-PSK")
                self.assertEqual(set(conf["wpa_pairwise"].split()), {"CCMP", "TKIP"})
                self.assertEqual(set(conf["rsn_pairwise"].split()), {"CCMP", "TKIP"})
                self.assertEqual(conf["ieee80211w"], "0")
                self.assertEqual(conf.get("bridge", ""), bridge)

    def test_existing_modes(self):
        for mode, key_mgmt, pmf in (("open", None, None), ("wpa2", "WPA-PSK", "1"),
                                    ("wpa2wpa3", "WPA-PSK SAE", "1"), ("wpa3", "SAE", "2")):
            with self.subTest(mode=mode):
                conf = self.config(mode)
                self.assertEqual(conf.get("wpa_key_mgmt"), key_mgmt)
                self.assertEqual(conf.get("ieee80211w"), pmf)
                if mode == "open":
                    self.assertNotIn("wpa", conf)
                else:
                    self.assertEqual(conf["wpa"], "2")
                    self.assertEqual(conf["rsn_pairwise"], "CCMP")
                    self.assertNotIn("wpa_pairwise", conf)
        self.assertNotIn("ieee80211w", self.config("wpa2", pmf="0"))

    def test_config_paths_and_escaping(self):
        password = "abc'defg$hi"
        for shared in (False, True):
            with self.subTest(shared=shared):
                self.base.joinpath("ap.conf").write_text("SECURITY='wpa3'\n")
                if shared:
                    self.shared.write_text("SECURITY='wpawpa2'\n")
                output = self.shell("load_conf; printf '%s\\n' \"$SECURITY\"")
                self.assertEqual(output.strip(), "wpawpa2" if shared else "wpa3")
                self.shell(f"SECURITY=wpawpa2; PASSWORD={shlex.quote(password)}; save_conf")
                output = self.shell("load_conf; printf '%s\\n%s\\n' \"$SECURITY\" \"$PASSWORD\"")
                self.assertEqual(output.splitlines(), ["wpawpa2", password])
                if shared:
                    self.assertEqual(self.base.joinpath("ap.conf").read_text(), "SECURITY='wpa3'\n")

    def test_cli_overrides_saved_mode(self):
        for saved, flags, expected in (("wpawpa2", [], "wpawpa2"),
                                       ("wpa3", ["-A", "wpawpa2"], "wpawpa2"),
                                       ("wpawpa2", ["-A", "wpa2"], "wpa2")):
            with self.subTest(saved=saved, flags=flags):
                self.shared.write_text(f"SECURITY='{saved}'\n")
                self.shell("cmd_start() { save_conf; }", cli=["start", *flags])
                output = self.shell("load_conf; printf '%s\\n' \"$SECURITY\"")
                self.assertEqual(output.strip(), expected)

    def test_status_uses_live_mode(self):
        for mode in ("open", "wpawpa2", "wpa2", "wpa2wpa3", "wpa3"):
            with self.subTest(mode=mode):
                self.config(mode)
                self.base.joinpath("run.state").write_text("ssid=test\n")
                output = self.shell('printf "%s\\n" "$$" > "$RUN_DIR/hostapd.pid"; '
                                    'SECURITY=wpa3; cmd_status')
                self.assertIn(f"security={mode}", output.splitlines())

    def test_compatibility_password_validation(self):
        for password, rejected in (("short", True), ("a" * 64, True), ("abcdefgh", False)):
            with self.subTest(length=len(password)):
                output = self.shell(
                    'stop_daemons() { :; }; check_prerequisites() { return 1; }; '
                    f'SSID=test; SECURITY=wpawpa2; PASSWORD={shlex.quote(password)}; cmd_start',
                    check=False
                )
                self.assertEqual("Password must be 8-63 characters." in output, rejected)

    def test_shell_syntax(self):
        subprocess.run(["sh", "-n", str(BACKEND)], check=True)
        busybox = shutil.which("busybox")
        if busybox:
            subprocess.run([busybox, "sh", "-n", str(BACKEND)], check=True)


if __name__ == "__main__":
    unittest.main()
