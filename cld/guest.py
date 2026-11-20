"""Shell access INTO a running VM, for the in-guest half of attachstorage.

Two ways in, tried in this order:

  1. qemu-guest-agent (QGA), reached through the compute node that hosts the VM:
     `ssh <compute> sudo -n docker exec nova_libvirt virsh qemu-agent-command ...`.
     Needs no guest SSH key and no network route to the VM -- only the operator's
     SSH key on the compute node (Kolla: libvirt lives in the nova_libvirt
     container) and a running agent in the guest. The compute node is tried by its
     service host name, then by the hypervisor's host_ip. Host keys are checked as
     usual (BatchMode, never StrictHostKeyChecking=no).
  2. SSH into the guest itself (user@address the operator names), with
     passwordless sudo there. Tenant networks are usually not routable from the
     controller, so this mostly works only via a floating IP.

Every command runs as root in the guest. Probing (connect_guest) is read-only:
`true`, `guest-ping`, `sudo -n true`.
"""

import base64
import getpass
import json
import os
import shlex
import subprocess
import tempfile
import time

from cld.cloud import safe_list
from cld.ui import out, warn, prompt_str

_VIRSH = "sudo -n docker exec nova_libvirt virsh -c qemu:///system "
_CONTROL_DIR = None


def _ssh_opts():
    """BatchMode (never prompt) + one shared connection per target, so the many
    small agent calls of one run reuse a single SSH session."""
    global _CONTROL_DIR
    if _CONTROL_DIR is None:
        _CONTROL_DIR = tempfile.mkdtemp(prefix="cld-ssh-")
    return ["-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "ControlMaster=auto", "-o", "ControlPersist=120",
            "-o", f"ControlPath={os.path.join(_CONTROL_DIR, '%C')}"]


def _ssh(target, remote_cmd, timeout=60):
    try:
        # stdin=DEVNULL: ssh would otherwise read (and swallow) the operator's
        # typed answers to the next prompts.
        r = subprocess.run(["ssh", *_ssh_opts(), target, remote_cmd],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, "ssh timed out"
    return r.returncode, (r.stdout + r.stderr).strip()


class GuestError(RuntimeError):
    """The way into the guest broke (agent gone, SSH dropped). The guest's state
    after the last command is unknown to cld -- callers must say so."""


class AgentGuest:
    kind = "guest agent"

    def __init__(self, compute, domain):
        self.compute, self.domain = compute, domain

    def describe(self):
        return f"qemu-guest-agent of {self.domain} via {self.compute}"

    def _agent(self, payload, timeout=60):
        rc, txt = _ssh(self.compute, _VIRSH + "qemu-agent-command "
                       + shlex.quote(self.domain) + " "
                       + shlex.quote(json.dumps(payload)), timeout=timeout)
        if rc != 0:
            raise GuestError(txt or f"virsh exit {rc}")
        try:
            return json.loads(txt)["return"]
        except (ValueError, KeyError) as e:
            raise GuestError(f"unexpected agent reply: {txt[:200]}") from e

    def ping(self):
        try:
            return self._agent({"execute": "guest-ping"}, timeout=20) == {}
        except GuestError:
            return False

    def run(self, cmd, timeout=60):
        """-> (exit code, stdout+stderr). Raises GuestError if the agent fails."""
        pid = self._agent({"execute": "guest-exec", "arguments": {
            "path": "/bin/sh", "arg": ["-c", cmd], "capture-output": True}})["pid"]
        deadline = time.monotonic() + timeout
        while True:
            s = self._agent({"execute": "guest-exec-status",
                             "arguments": {"pid": pid}})
            if s.get("exited"):
                text = "".join(base64.b64decode(s[k]).decode(errors="replace")
                               for k in ("out-data", "err-data") if s.get(k))
                return s.get("exitcode", -1), text.strip()
            if time.monotonic() > deadline:
                raise GuestError(f"command still running after {timeout}s: {cmd[:80]}")
            time.sleep(0.3)


class SshGuest:
    kind = "ssh"

    def __init__(self, target):
        self.target = target

    def describe(self):
        return f"ssh {self.target} (sudo)"

    def run(self, cmd, timeout=60):
        rc, txt = _ssh(self.target, "sudo -n sh -c " + shlex.quote(cmd), timeout)
        if rc == 255:
            raise GuestError(txt or "ssh connection failed")
        return rc, txt


def _compute_targets(conn, host):
    """SSH targets for the compute node `host`: its name, then its host_ip."""
    user = getpass.getuser()
    targets = [f"{user}@{host}"]
    for h in safe_list(conn.compute.hypervisors, details=True):
        svc = (getattr(h, "service_details", None) or {}).get("host")
        ip = getattr(h, "host_ip", None)
        if svc == host and ip and f"{user}@{ip}" not in targets:
            targets.append(f"{user}@{ip}")
    return targets


def _addresses(server):
    """Floating IPs first (the only ones usually routable from here), then fixed."""
    fl, fx = [], []
    for addrs in (getattr(server, "addresses", None) or {}).values():
        for a in addrs:
            (fl if a.get("OS-EXT-IPS:type") == "floating" else fx).append(a.get("addr"))
    return [a for a in fl + fx if a]


def connect_guest(conn, server, ask_ssh=True):
    """-> AgentGuest | SshGuest | None. Read-only probing; prints what it tried."""
    host = getattr(server, "compute_host", None)
    domain = getattr(server, "instance_name", None)
    if host and domain:
        for target in _compute_targets(conn, host):
            rc, txt = _ssh(target, "true", timeout=15)
            if rc != 0:
                out(f"[dim]  {target}: {txt.splitlines()[-1] if txt else 'no access'}[/dim]")
                continue
            g = AgentGuest(target, domain)
            if g.ping():
                out(f"Guest access: [bold]{g.describe()}[/bold]")
                return g
            warn(f"the qemu-guest-agent of {server.name} does not answer (via {target}).")
            break
    else:
        warn("the server's compute host / libvirt domain is not visible "
             "(needs the admin role); the guest agent cannot be used.")

    if not ask_ssh:
        return None
    addrs = _addresses(server)
    out(f"[dim]Fallback: SSH into {server.name} itself (needs a key that works there "
        f"and passwordless sudo). Addresses: {', '.join(addrs) or 'none'}.[/dim]")
    while True:
        target = prompt_str("SSH target for the guest, user@address (Enter = skip)",
                            required=False)
        if not target:
            return None
        g = SshGuest(target)
        try:
            rc, txt = g.run("true", timeout=20)
        except GuestError as e:
            rc, txt = -1, str(e)
        if rc == 0:
            out(f"Guest access: [bold]{g.describe()}[/bold]")
            return g
        warn(f"{target}: {txt.splitlines()[-1] if txt else 'failed'} -- try another, "
             "or Enter to skip")
