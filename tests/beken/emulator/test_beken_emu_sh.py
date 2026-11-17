"""beken-emu.sh hands the container its own process: a signal to the script - a
timeout's, a Ctrl-C - reaches the docker client and so the container, instead
of leaving both running behind a script that has gone."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools" / "beken" / "emulator" / "beken-emu.sh"
IMAGE_SH = ROOT / "tools" / "beken" / "emulator" / "image.sh"
DEVICE_NET = ROOT / "tools" / "beken" / "emulator" / "device-net.sh"


@unittest.skipIf(shutil.which("bash") is None, "no bash")
class ContainerIsExecedTests(unittest.TestCase):
    def test_docker_runs_as_the_script_itself(self):
        function = re.search(r"^container\(\) \{.*?^\}", SCRIPT.read_text(encoding="utf-8"), re.S | re.M).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp, "docker")
            fake.write_text("#!/bin/sh\necho \"docker pid $$: $*\"\n", encoding="utf-8")
            fake.chmod(0o755)
            script = ("set -u\nROOT=%s FIRMWARES=%s/firmwares IMAGE=img PUBLIC_DUMP=dump.bin\n%s\n"
                      "echo \"shell pid $$\"\ncontainer python -c pass\necho \"still here\"\n"
                      % (tmp, tmp, function))
            done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60,
                                  env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"}, cwd=tmp)
        self.assertEqual(done.returncode, 0, done.stderr)
        shell, docker = re.search(r"shell pid (\d+)", done.stdout), re.search(r"docker pid (\d+)", done.stdout)
        self.assertEqual(shell.group(1), docker.group(1), done.stdout)
        self.assertNotIn("still here", done.stdout)


@unittest.skipIf(shutil.which("bash") is None, "no bash")
class DeviceDumpTests(unittest.TestCase):
    def dump(self, own):
        function = re.search(r"^device_dump\(\) \{.*?^\}", SCRIPT.read_text(encoding="utf-8"), re.S | re.M).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.realpath(tmp)
            Path(root, "captures").mkdir()
            Path(root, "captures", "device.bin").touch()
            script = f"set -u\nROOT={root} PUBLIC_DUMP=dump.bin\n{function}\ndevice_dump\n"
            env = {**os.environ, "AUTOFLASH_BEKEN_EMU_FLASH": own.format(root=root)}
            if not own:
                del env["AUTOFLASH_BEKEN_EMU_FLASH"]
            return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60, env=env, cwd=tmp)

    def test_a_new_device_starts_from_the_public_dump(self):
        self.assertEqual(self.dump("").stdout.strip(), "/opt/beken-emulator/firmwares/dump.bin")

    def test_or_from_a_flash_under_the_checkout_as_the_container_sees_it(self):
        self.assertEqual(self.dump("{root}/captures/device.bin").stdout.strip(), "/work/captures/device.bin")

    def test_one_outside_the_checkout_is_refused(self):
        done = self.dump("/nowhere/device.bin")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("outside this checkout", done.stderr)


def english_locale():
    """A UTF-8 English locale this computer has, or None."""
    try:
        found = subprocess.run(["locale", "-a"], capture_output=True, text=True, timeout=10).stdout.split()
    except OSError:
        return None
    return next((name for name in found if name.lower() in ("en_gb.utf8", "en_us.utf8")), None)


@unittest.skipIf(shutil.which("bash") is None or english_locale() is None, "no bash or English locale")
class ImageTagTests(unittest.TestCase):
    """An emulator image's tag is the same on every computer: its files sorted
    by byte, not by the locale - an English one sorts UPSTREAM among the lower
    case names, C before them, and one computer's tag and the published image's
    did not match (seen setting up the S61's suite on other machines)."""

    def tags(self, script):
        function = re.search(r"^tag\(\) \{.*?^\}", script.read_text(encoding="utf-8"), re.S | re.M).group(0)
        here = script.parent
        found = []
        for locale in (english_locale(), "C"):
            done = subprocess.run(["bash", "-c", f"HERE={here} ROOT={ROOT}\n{function}\ntag"], capture_output=True,
                                  text=True, timeout=60, env={**os.environ, "LC_ALL": locale, "LANG": locale})
            self.assertEqual(done.returncode, 0, done.stderr)
            found.append(done.stdout.strip())
        return found

    def test_the_beken_images_tag_is_the_same_in_any_locale(self):
        english, c = self.tags(IMAGE_SH)
        self.assertEqual(english, c)

    def test_the_espressif_images_tag_is_the_same_in_any_locale(self):
        english, c = self.tags(ROOT / "tools" / "espressif" / "emulator" / "image.sh")
        self.assertEqual(english, c)


@unittest.skipIf(shutil.which("bash") is None or shutil.which("flock") is None, "no bash or flock")
class ImageLockTests(unittest.TestCase):
    def test_the_lock_is_let_go_once_the_image_is_there(self):
        # Held on, the docker run exec'd next kept it for its whole run, and
        # every other emulator's start waited behind it (seen in the fault suite):
        # image.sh, which takes it, runs as a process of its own.
        function = re.search(r"^ensure\(\) \{.*?^\}", SCRIPT.read_text(encoding="utf-8"), re.S | re.M).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "image.sh").write_text('#!/bin/bash\nexec 9>"$(dirname "$0")/lock"\nflock 9\n',
                                             encoding="utf-8")
            Path(tmp, "image.sh").chmod(0o755)
            script = ("set -u\nHERE=%s IMAGE=img\n%s\nensure\n"
                      "[ -e /proc/$$/fd/9 ] && echo held || echo free\n" % (tmp, function))
            done = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.stdout.strip(), "free", done.stderr)


@unittest.skipIf(shutil.which("bash") is None, "no bash")
class DeviceNetRouterTests(unittest.TestCase):
    """device-net.sh makes the container the device's home router: .1 on the
    network, the device's pool where Docker hands out nothing, and every
    connection the device makes off the network sent to the stub there."""

    def commands(self, address="198.18.61.130/24", then=None):
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp, "calls")
            state = Path(tmp, "state")
            state.mkdir()
            record = '#!/bin/sh\necho "$(basename "$0") $*" >> %s\n' % calls
            fakes = {
                "ip": record + "case \"$*\" in\n"
                      "  '-4 -o address show dev eth0') echo '2: eth0    inet %s brd x scope global eth0';;\n"
                      "esac\n" % address,
                "iptables": record, "dnsmasq": record, "setpriv": record, "chown": record,
                # The stub makes its CA, which device-net.sh waits for.
                "python3": record + "touch %s/ca.crt %s/leaf.crt %s/leaf.key\n" % (state, state, state),
            }
            for name, text in fakes.items():
                Path(tmp, name).write_text(text, encoding="utf-8")
                Path(tmp, name).chmod(0o755)
            done = subprocess.run(["bash", str(DEVICE_NET)], capture_output=True, text=True, timeout=60,
                                  env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}",
                                       "DEVICE_NET_RUN": str(Path(tmp, "run")), "DEVICE_STATE": str(state),
                                       "HOST_UID": "1000", "HOST_GID": "1000"})
            if then is not None:
                Path(tmp, "run", "leases").write_text("1 12:34:56:ab:cd:ef 198.18.61.50 device *\n", encoding="utf-8")
                calls.write_text("", encoding="utf-8")
                subprocess.run(["sh", str(Path(tmp, "run", "router")), *then], capture_output=True, timeout=30,
                               env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"})
                self.leases = Path(tmp, "run", "leases").read_text(encoding="utf-8")
            return done, calls.read_text(encoding="utf-8") if calls.exists() else ""

    def test_the_router_starts_again_forgetting_the_devices_address(self):
        # As a router may, when a device restarts: the fault suite's readdress.
        done, calls = self.commands(then=["55"])
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("--dhcp-range=198.18.61.55,198.18.61.59,", calls)
        self.assertEqual(self.leases, "")

    def test_the_container_is_the_devices_router_and_cloud(self):
        done, calls = self.commands()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("ip address add 198.18.61.1/24 dev br0", calls)
        dnsmasq = next(line for line in calls.splitlines() if line.startswith("dnsmasq "))
        self.assertIn("--dhcp-range=198.18.61.50,198.18.61.59,", dnsmasq)
        self.assertIn("--dhcp-option=option:router,198.18.61.1", dnsmasq)
        self.assertIn("--address=/#/198.18.61.1", dnsmasq)
        self.assertIn("-s 198.18.61.48/28 ! -d 198.18.61.0/24 -j DNAT --to-destination 198.18.61.1", calls)
        self.assertIn("--bind 198.18.61.1", calls)

    def test_the_devices_own_network_is_bridged_into_the_tunnel_with_no_address(self):
        done, calls = self.commands()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("ip link add own0 type vxlan id 42 group 239.255.42.1 dev br0 dstport 4789", calls)
        self.assertIn("ip link set tap1 master br1", calls)
        self.assertIn("ip link set own0 master br1", calls)
        self.assertNotIn("dev br1", calls.replace("master br1", ""))
        self.assertIn("--own-tap tap1", calls)

    def test_a_network_not_a_24_is_refused(self):
        done, calls = self.commands("10.0.0.5/16")
        self.assertNotEqual(done.returncode, 0)
        self.assertNotIn("br0", calls)


@unittest.skipIf(shutil.which("bash") is None, "no bash")
class ComputerWifiTests(unittest.TestCase):
    """The computer's Wi-Fi stand-ins (computer/bin/): the networks on the air
    as device.py writes them, and joining the device's own over the tunnel."""

    BIN = ROOT / "tools" / "beken" / "emulator" / "computer" / "bin"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.air = self.tmp / "air"
        self.air.mkdir()
        (self.air / "home").write_text("HomeNet\n", encoding="utf-8")
        (self.air / "station").write_text("02:00:00:00:00:02\n", encoding="utf-8")
        self.calls = self.tmp / "calls"
        record = '#!/bin/sh\necho "$(basename "$0") $*" >> %s\n' % self.calls
        fakes = {"ip": record + "[ \"$*\" = '-4 route show default' ] && echo 'default via 198.18.61.254 dev eth0'\n"
                       "exit 0\n",
                 "iptables": record + "case \"$*\" in -D*) exit 1;; esac\n",
                 # udhcpc's lease, as its script is given it.
                 "busybox": record + 'for script; do :; done\n'
                            'interface=own0 ip=192.168.4.2 mask=24 router=192.168.4.1 sh "$script" bound\n'}
        for name, text in fakes.items():
            (self.tmp / name).write_text(text, encoding="utf-8")
            (self.tmp / name).chmod(0o755)
        self.joins = self.tmp / "joins"
        self.joins.write_text("", encoding="utf-8")
        self.env = {**os.environ, "PATH": f"{self.tmp}:{os.environ['PATH']}",
                    "AUTOFLASH_EMULATOR_AIR": str(self.air), "AUTOFLASH_WIFI_RUN": str(self.tmp / "run"),
                    "AUTOFLASH_EMULATOR_JOINS": str(self.joins)}

    def run_bin(self, *args):
        return subprocess.run(["bash", str(self.BIN / args[0]), *args[1:]], capture_output=True, text=True,
                              timeout=30, env=self.env)

    def visible(self):
        return self.run_bin("nmcli", "-t", "-f", "SSID", "device", "wifi", "list").stdout.split()

    def test_the_scan_lists_both_networks_and_one_just_gone_for_a_minute(self):
        (self.air / "network").write_text("esphome-abcdef\n", encoding="utf-8")
        self.assertEqual(sorted(self.visible()), ["HomeNet", "esphome-abcdef"])
        (self.air / "network").unlink()
        self.assertIn("esphome-abcdef", self.visible(), "a scan remembers it, as a real one does")
        (self.tmp / "run" / "scan-cache").write_text("0\n", encoding="utf-8")
        self.assertEqual(self.visible(), ["HomeNet"])
        self.assertNotEqual(self.run_bin("nmcli", "connection", "show").returncode, 0)

    def test_only_the_network_on_the_air_is_joined(self):
        done = self.run_bin("wifi", "join", "esphome-abcdef")
        self.assertEqual(done.returncode, 1)
        self.assertIn("No network 'esphome-abcdef' is on the air", done.stderr)
        self.assertFalse(self.calls.exists(), "nothing changed")

    def test_joined_through_the_tunnel_off_the_home_network_then_home_again(self):
        (self.air / "network").write_text("esphome-abcdef\n", encoding="utf-8")
        done = self.run_bin("wifi", "join")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("Joined esphome-abcdef; the device is at 192.168.4.1.", done.stdout)
        self.assertRegex(self.joins.read_text(encoding="utf-8"), r"^join \d", "the station told to join afresh")
        calls = self.calls.read_text(encoding="utf-8")
        self.assertIn("ip link add own0 type vxlan id 42 group 239.255.42.1 dev eth0 dstport 4789", calls)
        self.assertIn("ip link set own0 address 02:00:00:00:00:02", calls)
        self.assertIn(f"iptables -I OUTPUT 1 -o eth0 -m owner --uid-owner {os.getuid()} -j DROP", calls)
        self.assertIn("iptables -I OUTPUT 1 -o eth0 -p udp --dport 4789 -j ACCEPT", calls)
        self.assertIn("ip address add 192.168.4.2/24 dev own0", calls)
        self.assertIn("ip route replace default via 192.168.4.1 dev own0", calls)
        self.assertIn("Joined:   esphome-abcdef", self.run_bin("wifi").stdout)
        self.calls.unlink()
        done = self.run_bin("wifi", "home")
        self.assertEqual(done.returncode, 0, done.stderr)
        calls = self.calls.read_text(encoding="utf-8")
        self.assertIn("ip link del own0", calls)
        self.assertRegex(self.joins.read_text(encoding="utf-8"), r"^home \d", "the station told to leave")
        self.assertIn(f"iptables -D OUTPUT -o eth0 -m owner --uid-owner {os.getuid()} -j DROP", calls)
        self.assertIn("ip route replace default via 198.18.61.254 dev eth0", calls)
        self.assertIn("Joined:   the home network", self.run_bin("wifi").stdout)


@unittest.skipIf(shutil.which("bash") is None, "no bash")
class ComputerHandOverTests(unittest.TestCase):
    """computer.sh, started as root, runs everything as your user with the
    right to move its own links and no more (setpriv's ambient capabilities)."""

    COMPUTER = ROOT / "tools" / "beken" / "emulator" / "computer.sh"

    def run_computer(self, known=True, **env):
        """computer.sh run with stand-ins: `known` whether the image has a user
        of your id already, else useradd makes one."""
        with tempfile.TemporaryDirectory() as tmp:
            calls, made = Path(tmp, "calls"), Path(tmp, "made")
            if known:
                made.touch()
            getent = (f'#!/bin/sh\n[ "$1" = group ] && exit 2\n[ -f {made} ] || exit 2\n'
                      "echo 'you:x:1001:1001::/home/you:/bin/sh'\n")
            for name, text in {"id": "#!/bin/sh\necho 0\n", "getent": getent,
                               "groupadd": f'#!/bin/sh\necho "groupadd $*" >> {calls}\n',
                               "useradd": f'#!/bin/sh\necho "useradd $*" >> {calls}\ntouch {made}\n',
                               "setpriv": f'#!/bin/sh\necho "$*" >> {calls}\n'}.items():
                Path(tmp, name).write_text(text, encoding="utf-8")
                Path(tmp, name).chmod(0o755)
            done = subprocess.run(["bash", str(self.COMPUTER), "tools/autoflash/cli.py", "run"], capture_output=True,
                                  text=True, timeout=30, env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}", **env})
            return done, calls.read_text(encoding="utf-8") if calls.exists() else ""

    def test_root_hands_over_to_your_user_with_its_links_rights(self):
        done, calls = self.run_computer(HOST_UID="1000", HOST_GID="1000")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(calls, "--reuid=1000 --regid=1000 --clear-groups --inh-caps +net_admin,+net_raw "
                                "--ambient-caps +net_admin,+net_raw env HOME=/home/you "
                                f"{self.COMPUTER} tools/autoflash/cli.py run\n")

    def test_your_user_is_made_where_the_image_has_none_of_your_id(self):
        # A CI runner's user is 1001; the image has only its own 1000.
        done, calls = self.run_computer(known=False, HOST_UID="1001", HOST_GID="1001")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(calls.splitlines()[:2], ["groupadd -g 1001 you", "useradd -m -u 1001 -g 1001 -s /bin/bash you"])
        self.assertIn("--reuid=1001 --regid=1001", calls)
        self.assertIn("env HOME=/home/you", calls)

    def test_without_your_group_it_does_not_start(self):
        done, calls = self.run_computer(HOST_UID="1000")
        self.assertNotEqual(done.returncode, 0)
        self.assertEqual(calls, "")


@unittest.skipIf(shutil.which("bash") is None, "no bash")
class NetworkRaceTests(unittest.TestCase):
    """Devices started side by side may choose the same free subnet: the one
    whose create loses chooses again (seen with eight at once on a VM)."""

    def make(self, creates_fail):
        function = re.search(r"^network\(\) \{.*?^\}", SCRIPT.read_text(encoding="utf-8"), re.S | re.M).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            calls = Path(tmp, "calls")
            docker = ("#!/bin/sh\necho \"$*\" >> %s\n"
                      "case \"$1 $2\" in\n"
                      "  'network inspect') [ -f %s/made ] ;;\n"
                      "  'network ls') : ;;\n"
                      "  'network create') n=$(grep -c 'network create' %s); [ \"$n\" -gt %d ] && touch %s/made ;;\n"
                      "esac\n" % (calls, tmp, calls, creates_fail, tmp))
            for name, text in {"docker": docker, "sleep": "#!/bin/sh\n", "ip": "#!/bin/sh\n"}.items():
                Path(tmp, name).write_text(text, encoding="utf-8")
                Path(tmp, name).chmod(0o755)
            done = subprocess.run(["bash", "-c", "set -u\nNETWORK=net\n%s\nnetwork" % function],
                                  capture_output=True, text=True, timeout=60,
                                  env={**os.environ, "PATH": f"{tmp}:{os.environ['PATH']}"})
            return done, calls.read_text(encoding="utf-8").count("network create")

    def test_a_lost_race_is_tried_again(self):
        done, creates = self.make(creates_fail=2)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(creates, 3)

    def test_it_gives_up_in_the_end(self):
        done, creates = self.make(creates_fail=99)
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("Could not make the Docker network net", done.stderr)


def checkout(tmp):
    """What beken-emu.sh needs of this checkout, copied under `tmp`."""
    for name in ("tools/beken/emulator/beken-emu.sh", "tools/beken/emulator/folders.py",
                 "tools/beken/emulator/image.sh", "tools/beken/emulator/UPSTREAM", "tools/repo/emulator-room.sh"):
        Path(tmp, name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, Path(tmp, name))


@unittest.skipIf(shutil.which("bash") is None, "no bash")
class OptionTests(unittest.TestCase):
    """beken-emu.sh spells the jobs it shares with emu.sh as emu.sh does:
    --instance N, --fresh, --rebuild."""

    def run_script(self, *args, before=None, lease=""):
        """beken-emu.sh ARGS, run from a copy of its checkout in a temporary
        directory, so what it does to captures/ is done there; Docker a fake
        that says what it was asked, and gives the device LEASE (a line of its
        router's leases), and ui_proxy.py one that says how it was started."""
        with tempfile.TemporaryDirectory() as tmp:
            checkout(tmp)
            calls = Path(tmp, "calls")
            Path(tmp, "bin").mkdir()
            Path(tmp, "bin", "docker").write_text(
                '#!/bin/sh\necho "$*" >> %s\ncase "$*" in *leases*) echo "%s" ;; esac\n' % (calls, lease),
                encoding="utf-8")
            Path(tmp, "bin", "python3").write_text(
                '#!/bin/sh\ncase "$*" in *ui_proxy.py*) echo "ui_proxy $*" >> %s; exit 0 ;; esac\nexec %s "$@"\n'
                % (calls, sys.executable), encoding="utf-8")
            for tool in ("docker", "python3"):
                Path(tmp, "bin", tool).chmod(0o755)
            if before:
                before(Path(tmp))
            done = subprocess.run(["bash", str(Path(tmp, SCRIPT.relative_to(ROOT))), *args], capture_output=True,
                                  text=True, timeout=60, env={**os.environ, "PATH": f"{tmp}/bin:{os.environ['PATH']}"})
            self.left = sorted(str(path.relative_to(tmp)) for path in Path(tmp, "captures").rglob("*"))
            router = Path(tmp, "captures/emulation/beken/state/router")
            self.router = router.read_text(encoding="utf-8") if router.exists() else None
            return done, calls.read_text(encoding="utf-8") if calls.exists() else ""

    def test_the_router_switch_is_the_devices_state_file(self):
        before = lambda tmp: Path(tmp, "captures/emulation/beken/state").mkdir(parents=True)
        for args, written in ((("off",), "off\n"), (("on",), "on\n"), (("on", "new-pass-1"), "on new-pass-1\n")):
            done, calls = self.run_script("router", *args, before=before)
            self.assertEqual((done.returncode, self.router), (0, written), done.stderr)
            self.assertEqual(calls, "", "nothing asked of Docker")
        done, _ = self.run_script("router", "up", before=before)
        self.assertEqual((done.returncode, self.router), (2, None))
        self.assertIn("router off, or on [PASSWORD]", done.stderr)

    def test_an_instance_is_its_own_device(self):
        done, calls = self.run_script("--instance", "2", "ui")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("exec autoflash-beken-emu-2 ", calls)

    def test_an_instance_out_of_range_is_refused(self):
        for instance in ("0", "100", "x"):
            done, calls = self.run_script("--instance", instance, "device")
            self.assertEqual(done.returncode, 2, instance)
            self.assertIn("1-99", done.stderr)
            self.assertEqual(calls, "", instance)

    def test_the_page_is_on_a_port_no_espressif_emulator_takes(self):
        # 8080+N is the Espressif emulator N's page, 8088 the tool's own bridge.
        for args, port in ((("ui",), "8200"), (("--instance", "9", "ui"), "8209"),
                           (("--instance", "99", "ui"), "8299")):
            done, calls = self.run_script(*args, lease="1 2 192.0.2.60 openbeken-abcdef *")
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn(f"--listen 127.0.0.1:{port}", calls, args)

    def test_instances_go_up_to_99(self):
        done, calls = self.run_script("--instance", "99", "ui")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn("exec autoflash-beken-emu-99 ", calls)

    def test_fresh_is_for_device_alone(self):
        done, calls = self.run_script("--fresh", "power", "on")
        self.assertEqual(done.returncode, 2)
        self.assertIn("--fresh is for device", done.stderr)
        self.assertEqual(calls, "")

    def test_the_old_spellings_are_gone(self):
        for args in (("reset",), ("ensure", "--rebuild"), ("--bogus", "device")):
            done, calls = self.run_script(*args)
            self.assertEqual(done.returncode, 2, args)
            self.assertIn("--instance N", done.stdout, args)
            self.assertEqual(calls, "", args)

    def test_an_earlier_checkouts_folders_are_moved_on_first_use(self):
        def old(tmp):
            Path(tmp, "captures/emulation/s61/autoflash-s61-2").mkdir(parents=True)
            Path(tmp, "captures/emulation/s61/autoflash-s61-2/flash.bin").write_bytes(b"device")
        done, _calls = self.run_script("--instance", "2", "ui", before=old)
        self.assertIn("s61/autoflash-s61-2 to beken/state-2", done.stderr)
        self.assertEqual(self.left, ["captures/emulation", "captures/emulation/beken",
                                     "captures/emulation/beken/state-2", "captures/emulation/beken/state-2/flash.bin"])

    def test_rebuild_reaches_image_sh(self):
        function = re.search(r"^ensure\(\) \{.*?^\}", SCRIPT.read_text(encoding="utf-8"), re.S | re.M).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "image.sh").write_text('#!/bin/sh\necho "$*"\n', encoding="utf-8")
            Path(tmp, "image.sh").chmod(0o755)
            found = [subprocess.run(["bash", "-c", f"set -u\nHERE={tmp} IMAGE=img rebuild={rebuild}\n{function}\nensure"],
                                    capture_output=True, text=True, timeout=60).stdout.strip()
                     for rebuild in ("--rebuild", '""')]
        self.assertEqual(found, ["ensure img --rebuild", "ensure img"])


@unittest.skipIf(shutil.which("bash") is None or shutil.which("rsync") is None or shutil.which("git") is None,
                 "no bash, rsync or git")
class ComputerApiTests(unittest.TestCase):
    """The computer's readouts go to the API's own code in a container beside
    the device, one a device, so both sides of a readout are seen here; with
    --no-api, kept unsent. Docker a fake that says what it was asked."""

    def run_computer(self, *options, api_running=False):
        with tempfile.TemporaryDirectory() as tmp:
            checkout(tmp)
            shutil.copy2(ROOT / "tools/beken/emulator/computer.sh", Path(tmp, "tools/beken/emulator/computer.sh"))
            Path(tmp, "tools/beken/emulator/image.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            git = ["git", "-C", tmp, "-c", "user.name=t", "-c", "user.email=t@example.com"]
            subprocess.run([*git, "init", "--quiet"], check=True)
            subprocess.run([*git, "add", "."], check=True)
            state = Path(tmp, "captures/emulation/beken/state")
            state.mkdir(parents=True)
            (state / "ca.crt").write_text("ca\n", encoding="utf-8")
            calls = Path(tmp, "calls")
            Path(tmp, "bin").mkdir()
            Path(tmp, "bin", "docker").write_text(
                '#!/bin/sh\necho "$*" >> %s\n'
                'case "$*" in\n'
                '  "network inspect -f {{range .IPAM.Config}}{{.IPRange}}{{end}} "*) echo 198.18.61.128/25 ;;\n'
                '  "network inspect -f {{range .IPAM.Config}}{{.Subnet}}{{end}} "*) echo 198.18.61.0/24 ;;\n'
                '  "ps -q --filter name=^autoflash-beken-emu-api$") %s ;;\n'
                '  "logs "*) echo "API on 0.0.0.0:8080" ;;\n'
                '  "inspect -f {{.State.Running}} "*) echo true ;;\n'
                '  "inspect -f {{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}} "*) echo 192.0.2.7 ;;\n'
                'esac\n' % (calls, "echo abc123" if api_running else ":"), encoding="utf-8")
            Path(tmp, "bin", "docker").chmod(0o755)
            done = subprocess.run(["bash", str(Path(tmp, SCRIPT.relative_to(ROOT))), *options, "computer", "x.py"],
                                  capture_output=True, text=True, timeout=60,
                                  env={**os.environ, "PATH": f"{tmp}/bin:{os.environ['PATH']}"})
            self.assertEqual(done.returncode, 0, done.stderr)
            asked = calls.read_text(encoding="utf-8").splitlines()
            return asked, next(line for line in asked if line.startswith("run --rm -i"))

    def test_by_default_its_readouts_go_to_the_api_beside_the_device(self):
        asked, computer = self.run_computer()
        api = next(line for line in asked if line.startswith("run -d --name autoflash-beken-emu-api"))
        self.assertIn("--network autoflash-beken-emu ", api)
        self.assertIn("node /website/api/local.mjs 0.0.0.0 8080 /api", api)
        self.assertIn("-e AUTOFLASH_READOUT_URL=http://192.0.2.7:8080/v1/readout", computer)

    def test_a_second_computer_finds_the_first_ones_api(self):
        # One a device: a second readout finds the first's, as on the website.
        asked, computer = self.run_computer(api_running=True)
        self.assertFalse([line for line in asked if line.startswith("run -d")])
        self.assertIn("-e AUTOFLASH_READOUT_URL=http://192.0.2.7:8080/v1/readout", computer)

    def test_the_computer_starts_as_root_with_the_air_the_router_and_its_rights(self):
        _, computer = self.run_computer()
        state = "captures/emulation/beken/state"
        self.assertIn("--user 0:0", computer)
        self.assertIn("--cap-add NET_ADMIN", computer)
        self.assertRegex(computer, rf"-v \S+/{state}/joins:/device/joins")
        self.assertIn(f"-e HOST_UID={os.getuid()} -e HOST_GID={os.getgid()}", computer)
        self.assertRegex(computer, rf"-v \S+/{state}/air:/device/air:ro")
        self.assertRegex(computer, rf"-v \S+/{state}/router:/device/router -e AUTOFLASH_EMULATOR_ROUTER=/device/router")

    def test_no_api_keeps_its_readouts_unsent(self):
        asked, computer = self.run_computer("--no-api")
        self.assertFalse([line for line in asked if "local.mjs" in line])
        self.assertIn("-e AUTOFLASH_READOUT_URL= ", computer)


@unittest.skipIf(shutil.which("bash") is None, "no bash")
class ToolSettingsTests(unittest.TestCase):
    """A tool's settings (cloud_dial.py's DISPATCH, seed_join.py's SSID, ...) are
    the NAME=VALUE arguments python and run are given first, as env takes them.
    Nothing else of the environment reaches the container: a SERVER or PASS set
    in a shell for something else once did, and changed what a tool did."""

    def docker(self, *args, **env):
        """What beken-emu.sh ARGS, run with ENV, gives the container: its
        settings (each -e but the script's own dump) and its command."""
        with tempfile.TemporaryDirectory() as tmp:
            checkout(tmp)
            Path(tmp, "tools/beken/emulator/image.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            firmwares = Path(tmp, "captures/emulation/beken/firmwares")
            firmwares.mkdir(parents=True)
            Path(firmwares, "Sonoff_S61s_EUPlug_WBBK_01P_V1.3.bin").touch()
            Path(tmp, "bin").mkdir()
            Path(tmp, "bin", "docker").write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n', encoding="utf-8")
            Path(tmp, "bin", "docker").chmod(0o755)
            done = subprocess.run(["bash", str(Path(tmp, SCRIPT.relative_to(ROOT))), *args], capture_output=True,
                                  text=True, timeout=60, cwd=tmp,
                                  env={**os.environ, **env, "AUTOFLASH_BEKEN_EMU_IMAGE": "img",
                                       "PATH": f"{tmp}/bin:{os.environ['PATH']}"})
        self.assertEqual(done.returncode, 0, done.stderr)
        given = done.stdout.splitlines()
        image = given.index("img")
        settings = [given[i + 1] for i in range(image) if given[i] == "-e"]
        settings.remove("AUTOFLASH_BEKEN_EMU_DUMP=/opt/beken-emulator/firmwares/Sonoff_S61s_EUPlug_WBBK_01P_V1.3.bin")
        return settings, given[image + 1:]

    def test_a_setting_reaches_the_tool(self):
        self.assertEqual(self.docker("python", "DISPATCH=http://192.0.2.1:80", "cloud_dial.py"),
                         (["DISPATCH=http://192.0.2.1:80"], ["cloud_dial.py"]))

    def test_one_set_empty_arrives_empty(self):
        # cloud_dial.py's DISPATCH= is the factory placeholder, not its default.
        self.assertEqual(self.docker("python", "DISPATCH=", "cloud_dial.py"), (["DISPATCH="], ["cloud_dial.py"]))

    def test_several_arrive_in_order_spaces_and_all(self):
        self.assertEqual(self.docker("python", "SSID=My Net", "PASS=a=b", "SECS=40", "seed_join.py"),
                         (["SSID=My Net", "PASS=a=b", "SECS=40"], ["seed_join.py"]))

    def test_none_given_none_passed(self):
        self.assertEqual(self.docker("python", "cloud_dial.py"), ([], ["cloud_dial.py"]))

    def test_the_environment_does_not_reach_the_tool(self):
        self.assertEqual(self.docker("python", "SECS=5", "cloud_dial.py", SERVER="x", PASS="x", DISPATCH="x"),
                         (["SECS=5"], ["cloud_dial.py"]))

    def test_one_after_the_tool_is_the_tools_argument(self):
        self.assertEqual(self.docker("python", "cloud_dial.py", "SECS=5"), ([], ["cloud_dial.py", "SECS=5"]))
        self.assertEqual(self.docker("python", "-c", "x=1", "Y=2"), ([], ["-c", "x=1", "Y=2"]))
        self.assertEqual(self.docker("python", "SECS=5", "-c", "x=1"), (["SECS=5"], ["-c", "x=1"]))

    def test_a_setting_given_wins_over_the_scripts_own(self):
        # A dump of your own, in place of the public one the script names.
        settings, _command = self.docker("python", "AUTOFLASH_BEKEN_EMU_DUMP=/work/captures/own.bin", "seed_join.py")
        self.assertEqual(settings, ["AUTOFLASH_BEKEN_EMU_DUMP=/work/captures/own.bin"])

    def test_run_takes_them_before_its_dump(self):
        dump = "captures/emulation/beken/firmwares/Sonoff_S61s_EUPlug_WBBK_01P_V1.3.bin"
        settings, command = self.docker("run", "BK_MONITOR_RX=1", "SECS=", dump, "--wifi-peer", "auto", "X=1",
                                        PINS="0 2")
        self.assertEqual(settings, ["BK_MONITOR_RX=1", "SECS="])
        self.assertEqual(command[0], "/opt/beken-emulator/src/main.py")
        self.assertTrue(command[1].endswith("/" + dump), command)
        self.assertEqual(command[2:], ["--with-boot", "-chip", "BK7238", "--only-uart", "--wifi-peer", "auto", "X=1"])

    def test_run_without_a_dump_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout(tmp)
            done = subprocess.run(["bash", str(Path(tmp, SCRIPT.relative_to(ROOT))), "run", "SECS=5"],
                                  capture_output=True, text=True, timeout=60, cwd=tmp)
        self.assertEqual(done.returncode, 2)
        self.assertIn("run needs a dump", done.stderr)


if __name__ == "__main__":
    unittest.main()
