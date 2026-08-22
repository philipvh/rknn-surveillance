# Copyright 2026 Philip van Houtte, magicview.tv, the Netherlands
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy
# of the License at http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. This
# software aids surveillance; it does not guarantee it, and no liability is
# accepted for any failure to detect, record, retain or report an event.
# See the NOTICE file for the full disclaimer.

"""Tests for publishing the camera UI over the tunnel.

camera_ui.sh runs as root and edits the packet-filter, so it cannot run in
the suite for real. Instead iptables/iptables-save/netfilter-persistent are
stubbed onto PATH and the script's decisions are read back from the commands
it tried to issue. What matters here is not the exact syntax of a rule but
that the right rules are added, the dead camera's rule is cleared rather than
left to rot, --remove undoes what --apply did, and a camera configured onto
the wrong segment is refused before it becomes a browser mystery.
"""
import os
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "camera_ui.sh"

# One live-but-dead INSTAR forward on the LAN, and an unrelated forward that
# must be left alone.
NAT_TABLE = (
    "-A PREROUTING -i enP4p65s0 -p tcp -m tcp --dport 8080 "
    "-j DNAT --to-destination 192.168.91.200:80\n"
    "-A PREROUTING -i enP4p65s0 -p tcp -m tcp --dport 9000 "
    "-j DNAT --to-destination 192.168.8.50:9000\n"
)


def run(args, cam_ip="192.168.91.47", cam_port="88", exists=False,
        forward_policy="DROP"):
    """Run camera_ui.sh against stubbed tools; return (rc, stdout, issued).

    `issued` is the list of iptables/netfilter-persistent invocations the
    script actually made -- the 'would:' dry-run lines are not counted, so a
    test must pass --apply (or --save/--remove) to see anything here.
    `exists` makes every -C existence check succeed, which is how a re-run and
    the --remove path are exercised.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        bind = tmp / "bin"
        bind.mkdir()
        log = tmp / "log"
        log.touch()

        (bind / "iptables").write_text(textwrap.dedent("""\
            #!/usr/bin/env bash
            args="$*"
            if [[ "$1" == "-C" || "$args" == *" -C "* ]]; then
              %s
            fi
            if [[ "$1" == "-L" && "$2" == "FORWARD" ]]; then
              echo "Chain FORWARD (policy %s)"; exit 0
            fi
            echo "IPT $args" >> "$LOGF"; exit 0
            """ % ("exit 0" if exists else "exit 1", forward_policy)))
        (bind / "iptables-save").write_text(
            "#!/usr/bin/env bash\ncat <<'R'\n%sR\n" % NAT_TABLE)
        (bind / "netfilter-persistent").write_text(
            '#!/usr/bin/env bash\necho "IPT netfilter-persistent $*" >> "$LOGF"\n')
        (bind / "id").write_text(
            '#!/usr/bin/env bash\n[ "$1" = "-u" ] && echo 0 || /usr/bin/id "$@"\n')
        (bind / "ip").write_text(
            '#!/usr/bin/env bash\n'
            '[[ "$*" == *"addr show tun0"* ]] && '
            'echo "6: tun0    inet 10.8.2.10 peer 10.8.2.9/32 scope global tun0" || true\n')
        for f in bind.iterdir():
            f.chmod(0o755)

        env = dict(os.environ)
        env.update(PATH="%s:%s" % (bind, env["PATH"]), LOGF=str(log),
                   CAM_IP=cam_ip, CAM_PORT=cam_port)
        p = subprocess.run(["bash", str(SCRIPT)] + args, env=env,
                           capture_output=True, text=True, timeout=30)
        issued = [ln[4:] for ln in log.read_text().splitlines()
                  if ln.startswith("IPT ")]
        # die() writes to stderr; callers assert on messages either way.
        return p.returncode, p.stdout + p.stderr, issued


class TestCameraUiForward(unittest.TestCase):

    def test_dry_run_changes_nothing(self):
        rc, out, issued = run([])            # no --apply
        self.assertEqual(rc, 0, out)
        self.assertEqual(issued, [], "a dry run issued real commands")
        self.assertIn("would:", out)

    def test_forwards_on_both_lan_and_tunnel(self):
        _, _, issued = run(["--apply"])
        adds = [c for c in issued if "-I PREROUTING" in c
                and "192.168.91.47:88" in c]
        ifaces = {c.split("-i ", 1)[1].split()[0] for c in adds}
        self.assertIn("enP4p65s0", ifaces)   # club LAN
        self.assertIn("tun+", ifaces)        # the VPN -- the whole point

    def test_clears_the_dead_camera_rule(self):
        # The INSTAR is gone; its forward must be deleted, not stacked under a
        # second rule on the same port.
        _, _, issued = run(["--apply"])
        self.assertTrue(any("-D PREROUTING" in c and "192.168.91.200" in c
                            for c in issued),
                        "the dead INSTAR forward was not cleared")

    def test_leaves_unrelated_forwards_alone(self):
        _, _, issued = run(["--apply"])
        self.assertFalse(any("192.168.8.50" in c for c in issued),
                         "an unrelated forward was touched")

    def test_masquerade_and_forward_path_added(self):
        _, _, issued = run(["--apply"])
        self.assertTrue(any("POSTROUTING" in c and "MASQUERADE" in c
                            for c in issued))
        self.assertTrue(any("-I FORWARD" in c and "tun+" in c for c in issued))
        self.assertTrue(any("RELATED,ESTABLISHED" in c for c in issued))

    def test_no_forward_accepts_when_policy_is_accept(self):
        # If Docker is not dropping, the accepts are noise; do not add them.
        _, _, issued = run(["--apply"], forward_policy="ACCEPT")
        self.assertFalse(any("-I FORWARD" in c for c in issued))

    def test_save_persists(self):
        _, _, issued = run(["--save"])
        self.assertTrue(any("netfilter-persistent save" in c for c in issued))

    def test_apply_alone_does_not_persist(self):
        _, out, issued = run(["--apply"])
        self.assertFalse(any("netfilter-persistent" in c for c in issued))
        self.assertIn("NOT saved", out)

    def test_remove_undoes_the_forward(self):
        _, _, issued = run(["--remove"], exists=True)
        dels = [c for c in issued if "-D PREROUTING" in c
                and "192.168.91.47:88" in c]
        ifaces = {c.split("-i ", 1)[1].split()[0] for c in dels}
        self.assertEqual(ifaces, {"enP4p65s0", "tun+"})

    def test_remove_leaves_the_shared_masquerade(self):
        # wan_ports may still want it for the recording forwards.
        _, _, issued = run(["--remove"], exists=True)
        self.assertFalse(any("MASQUERADE" in c for c in issued))

    def test_re_apply_is_idempotent(self):
        # Everything already present -> no PREROUTING adds.
        _, out, issued = run(["--apply"], exists=True)
        self.assertFalse(any("-I PREROUTING" in c for c in issued))
        self.assertIn("already there", out)

    def test_camera_off_segment_is_refused(self):
        rc, out, issued = run(["--apply"], cam_ip="192.168.8.99")
        self.assertNotEqual(rc, 0)
        self.assertIn("not on", out + "")
        self.assertEqual(issued, [], "issued rules despite a bad config")

    def test_prints_the_tunnel_url(self):
        _, out, _ = run(["--apply"])
        self.assertIn("http://10.8.2.10:8080/", out)


if __name__ == "__main__":
    unittest.main()
