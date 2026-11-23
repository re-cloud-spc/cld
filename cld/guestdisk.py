"""attachstorage, in-guest half: find the attached disk, partition, format, mount.

Runs through cld.guest (qemu-guest-agent, or SSH as a fallback). Three phases:

  inspect_disk  READ-ONLY. Finds the disk ONLY by its virtio serial
                (/dev/disk/by-id/virtio-<vol-id[:20]>, never Nova's /dev/vdX
                guess) and gives a verdict: blank / has-data / clash / mounted /
                root / missing. `clash` = it shares a LABEL/UUID/PARTUUID that the
                VM boots or mounts by (an old cloud-image root disk): after a
                reboot the VM may come up on THIS disk.
  plan_disk     Interview, no changes: wipe (only with the typed volume ID),
                whole disk or split into partitions, filesystem + mount point +
                who may write + fstab for each one; or, keeping the data, which
                existing partition mounts where (read-only by default).
  apply_plans   Re-checks everything, asks once more (default No), then wipes /
                partitions / formats / mounts / appends to fstab. fstab is backed up
                and verified with `findmnt --verify`; a failed verify restores it.

Moving a folder onto a new filesystem (/home/<user>, /var/log, /var/lib/<name>,
/var/tmp, /var/www, /srv /opt /data subfolders, /tmp): the folder must be a plain directory
with no mounts below it, fit on the disk and be used by no process (for /var/log
journald and rsyslog are released and re-opened by cld). The data is copied to the
new filesystem, the copy is compared, then the folder is renamed to
<folder>.cld-old-<time> and the new filesystem mounted in its place. The old copy is
NEVER deleted by cld. /tmp is not copied: it is prepared (1777) and switches at the
next reboot. Never /, /boot, /etc, /usr or all of /var.

Hard rules: never the root disk or a mounted disk; never format a disk with data
unless its wipe was confirmed by typing its ID; only ever ADD fstab lines (by-id
path + nofail, never LABEL=/UUID=, which cloned volumes share); never mount over a
non-empty directory (a moved folder is renamed away first, never hidden); never
touch grub, the kernel cmdline or the VM's own disk.
"""

import os
import re
import shlex

from cld import audit
from cld.guest import GuestError
from cld.ui import (out, header, warn, err, render_table, choose, confirm,
                    confirm_destructive, prompt_int, prompt_str, Abort, _ask_text)

LINUX_GUID = "0FC63DAF-8483-4772-8E79-3D69D8477DE4"   # GPT "Linux filesystem data"
_MP_RE = re.compile(r"/(mnt|srv|data|opt)(/[A-Za-z0-9._-]+)+")
_SEG = r"/[A-Za-z0-9._-]+"
_MOVE_RE = re.compile(rf"/tmp|/var/tmp|/var/log({_SEG})?|/var/www({_SEG})?|/var/lib({_SEG})+"
                      rf"|/home/[a-z_][a-z0-9_.-]*|/(srv|opt|data)({_SEG})+")
_LOG_WRITERS = {"systemd-journal", "rsyslogd"}   # released / re-opened by cld
_MAX_PARTS = 8


def byid(volume_id):
    return f"/dev/disk/by-id/virtio-{volume_id[:20]}"


def _q(s):
    return shlex.quote(s)


def _gb(n):
    try:
        n = int(n)
        if n >= 1024 ** 3:
            return f"{n / 1024 ** 3:.1f}G"
        return f"{n / 1024 ** 2:.0f}M" if n >= 1024 ** 2 else "<1M"
    except (TypeError, ValueError):
        return "?"


def _pairs(text):
    """`lsblk -P` / `blkid -o export`-style output -> list of dicts."""
    rows = []
    for line in text.splitlines():
        row = dict(re.findall(r'([A-Z:_-]+)="([^"]*)"', line))
        if row:
            rows.append(row)
    return rows


def _blkid_ids(text):
    out_, cur = [], {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                out_.append(cur)
            cur = {}
            continue
        k, _, v = line.partition("=")
        cur[k] = v
    if cur:
        out_.append(cur)
    return out_


# --------------------------------------------------------------------------- #
# Read-only inspection
# --------------------------------------------------------------------------- #
def boot_ids(g):
    """Identifiers the running VM boots/mounts by. A new disk must share none.

    Every LABEL/UUID/PARTUUID referenced by /proc/cmdline, /etc/fstab or grub.cfg,
    plus the UUID/PARTUUID of what is mounted at / /boot /boot/efi.
    """
    ids = set()
    _, src = g.run("for m in / /boot /boot/efi; do findmnt -no SOURCE $m; done 2>/dev/null")
    for dev in src.split():
        _, e = g.run(f"blkid -o export {_q(dev)}")
        for d in _blkid_ids(e):
            ids |= {f"{k}={d[k]}" for k in ("UUID", "PARTUUID") if k in d}
    _, refs = g.run("cat /proc/cmdline /etc/fstab /boot/grub/grub.cfg "
                    "/boot/grub2/grub.cfg 2>/dev/null")
    for m in re.finditer(r"\b(LABEL|UUID|PARTUUID)=([^\s,\"']+)", refs):
        ids.add(f"{m.group(1)}={m.group(2)}")
    for m in re.finditer(r"--fs-uuid --set=\S+ ([0-9A-Fa-f-]+)", refs):
        ids.add(f"UUID={m.group(1)}")
    return ids


def root_disk(g):
    """Kernel name of the disk holding / (e.g. 'vda')."""
    _, src = g.run("findmnt -no SOURCE /")
    _, pk = g.run(f"lsblk -no PKNAME {_q(src.strip())} 2>/dev/null | head -1")
    return pk.strip() or os.path.basename(src.strip())


def inspect_disk(g, volume_id, protected, rootdev, wait=60):
    """-> dict with verdict, kernel device, size and partitions. Read-only."""
    path = byid(volume_id)
    info = {"volume_id": volume_id, "byid": path, "dev": None, "parts": [],
            "size": 0, "clash": [], "verdict": "missing", "signatures": ""}
    # The attach returns before the guest has seen the disk: wait for udev.
    rc, dev = g.run(f"for i in $(seq {wait}); do [ -b {path} ] && break; sleep 1; "
                    f"done; readlink -f {path}", timeout=wait + 30)
    if rc != 0 or not dev.startswith("/dev/") or dev == path:
        return info
    info["dev"] = dev
    if os.path.basename(dev) == rootdev:
        info["verdict"] = "root"
        return info
    _, size = g.run(f"blockdev --getsize64 {dev}")
    info["size"] = int(size) if size.strip().isdigit() else 0
    _, ls = g.run(f"lsblk -P -b -o NAME,SIZE,TYPE,FSTYPE,LABEL,UUID,PARTUUID,MOUNTPOINT {dev}")
    rows = _pairs(ls)
    mounted = [r["MOUNTPOINT"] for r in rows if r.get("MOUNTPOINT")]
    for r in rows[1:]:
        if r.get("TYPE") != "part":
            continue
        n = re.search(r"(\d+)$", r["NAME"])
        info["parts"].append({
            "name": r["NAME"], "num": int(n.group(1)) if n else None,
            "path": f"{path}-part{n.group(1)}" if n else f"/dev/{r['NAME']}",
            "size": int(r.get("SIZE") or 0), "fstype": r.get("FSTYPE", ""),
            "label": r.get("LABEL", ""), "uuid": r.get("UUID", "")})
    whole = rows[0] if rows else {}
    info["fstype"], info["label"] = whole.get("FSTYPE", ""), whole.get("LABEL", "")
    info["uuid"] = whole.get("UUID", "")
    _, sig = g.run(f"wipefs -n {dev} 2>/dev/null; for p in {dev}?*; do [ -b $p ] && "
                   f"wipefs -n $p; done 2>/dev/null")
    _, ex = g.run(f"blkid -o export {dev} {dev}?* 2>/dev/null")
    info["signatures"] = sig.strip()
    info["clash"] = sorted({f"{k}={d[k]}" for d in _blkid_ids(ex)
                            for k in ("LABEL", "UUID", "PARTUUID") if k in d} & protected)
    if mounted:
        info["verdict"], info["mounted"] = "mounted", mounted
    elif info["clash"]:
        info["verdict"] = "clash"
    elif not sig.strip() and not info["parts"] and not ex.strip():
        info["verdict"] = "blank"
    else:
        info["verdict"] = "has-data"
    return info


def show_disk(info):
    name = f"{info['dev'] or '?'}  ({info['byid']})"
    rows = [[p["name"], _gb(p["size"]), p["fstype"] or "-", p["label"] or "-",
             p["uuid"] or "-"] for p in info["parts"]]
    if not rows:
        rows = [[os.path.basename(info["dev"] or "?"), _gb(info["size"]),
                 info.get("fstype") or "-", info.get("label") or "-",
                 info.get("uuid") or "-"]]
    render_table(f"In the guest: {name}", ["device", "size", "filesystem", "label",
                                          "uuid"], rows)
    verdict = {"blank": "[green]blank (no partitions, no signatures)[/green]",
               "has-data": "[yellow]has partitions / filesystems[/yellow]",
               "clash": "[red]BOOT CLASH: shares " + ", ".join(info["clash"])
                        + " with the VM's own boot disk[/red]",
               }.get(info["verdict"], info["verdict"])
    out(f"  verdict: {verdict}")


# --------------------------------------------------------------------------- #
# Interview (no changes)
# --------------------------------------------------------------------------- #
def confirm_wipe(volume, why):
    """Typed-ID consent to destroy a volume's contents. Quitting = keep."""
    name = getattr(volume, "name", "") or "(unnamed)"
    warn(f"{why}")
    if not confirm_destructive(f"Wipe ALL data on {name} ({volume.id}, "
                               f"{getattr(volume, 'size', '?')} GB)? This cannot be "
                               "undone.", default=False):
        return False
    try:
        typed = _ask_text(f"Type the first 8 characters of the volume ID "
                          f"({volume.id[:8]}) to confirm the wipe: ")
    except Abort:
        typed = None
    if typed != volume.id[:8]:
        out("Confirmation did not match; the data is kept.")
        return False
    audit.audit("guest.wipe.consent", id=volume.id)
    return True


def _free_mountpoint(g, taken):
    for i in range(1, 100):
        mp = f"/mnt/data{i}"
        if mp in taken:
            continue
        rc, _ = g.run(f"[ -e {mp} ]")
        if rc != 0:
            return mp
    return None


def _overlaps(path, taken):
    return any(path == t or path.startswith(t + "/") or t.startswith(path + "/")
               for t in taken)


def _users(g, path, kind):
    """Processes (cwd/root/exe/open files/mapped files) and unix sockets under
    `path`. READ-ONLY. -> (all users, blockers); for /var/log journald and rsyslog
    are not blockers, cld releases them."""
    _, txt = g.run(
        f"D={_q(path)}; pids=$( (find /proc/[0-9]*/cwd /proc/[0-9]*/root "
        f"/proc/[0-9]*/exe /proc/[0-9]*/fd -maxdepth 1 \\( -lname \"$D\" -o -lname "
        f"\"$D/*\" \\) 2>/dev/null; grep -lF \" $D/\" /proc/[0-9]*/maps 2>/dev/null) "
        f"| cut -d/ -f3 | sort -un); for p in $pids; do echo \"$p $(cat /proc/$p/comm "
        f"2>/dev/null)\"; done; awk -v d=\"$D\" 'NR > 1 && ($NF == d || "
        f"index($NF, d \"/\") == 1) {{print \"socket\", $NF}}' /proc/net/unix",
        timeout=120)
    users = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    ok = _LOG_WRITERS if kind == "log" else set()
    return users, [u for u in users if u.split(" ", 1)[-1] not in ok]


def _move_problem(g, path, cap):
    """Why `path` cannot be moved onto a filesystem of `cap` bytes, or None.
    READ-ONLY. -> (problem, bytes used)."""
    q = _q(path)
    rc, _ = g.run(f"[ -d {q} ] && [ ! -L {q} ]")
    if rc != 0:
        return "is not an existing folder (or is a symlink)", 0
    rc, _ = g.run(f"findmnt -rn --mountpoint {q}")
    if rc == 0:
        return "is already a mount point (e.g. a tmpfs)", 0
    _, sub = g.run(f"findmnt -rno TARGET | awk -v d={_q(path + '/')} 'index($0, d) == 1'")
    if sub.strip():
        return f"has mounts below it ({', '.join(sub.split()[:3])})", 0
    if path == "/tmp":
        return None, 0
    _, du = g.run(f"du -sx --block-size=1 {q} 2>/dev/null | cut -f1", timeout=600)
    if not du.strip().isdigit():
        return "its size could not be read", 0
    used = int(du)
    if used > cap * 0.85:          # filesystem overhead + ext4's 5% root reserve
        return f"holds {_gb(used)}, too much for {_gb(cap)}", used
    return None, used


def _ask_move(g, taken, cap):
    """An existing folder to move onto the new filesystem. None = none."""
    out("[dim]  Allowed: /home/<user>, /var/log, /var/lib/<name>, /var/tmp, /var/www, folders "
        "under /srv /opt /data, /tmp. Never /, /boot, /etc, /usr or all of /var.[/dim]")
    while True:
        p = prompt_str("  Folder to move onto this disk (Enter = none)", required=False)
        if not p:
            return None
        p = p.rstrip("/") or "/"
        if p == "/var":
            warn("all of /var is refused on a running VM (in use all the time, needed "
                 "early at boot); move /var/log or /var/lib/<name> instead")
            continue
        if not _MOVE_RE.fullmatch(p) or ".." in p:
            warn(f"{p} cannot be moved by cld")
            continue
        if _overlaps(p, taken):
            warn(f"{p} overlaps a folder already in this plan")
            continue
        prob, used = _move_problem(g, p, cap)
        if prob:
            warn(f"{p} {prob}")
            continue
        kind = "tmp" if p == "/tmp" else "log" if p == "/var/log" else "data"
        m = {"mp": p, "move": kind, "used": used}
        if kind == "tmp":
            m["chmod"] = "1777"
            out("[dim]  /tmp is not copied: the new filesystem is prepared (1777, "
                "nosuid, nodev) and takes over /tmp at the next reboot.[/dim]")
            return m
        out(f"  {p}: {_gb(used)} to copy; the old folder is kept as "
            f"{p}.cld-old-<time> on the VM's own disk.")
        users, blockers = _users(g, p, kind)
        if blockers:
            warn(f"{p} is in use by: {'; '.join(blockers[:8])}. Stop these (e.g. log "
                 "the user out, systemctl stop <service>) before Apply, or cld stops "
                 "there and changes nothing.")
        elif users:
            out(f"[dim]  {', '.join(sorted({u.split(' ', 1)[-1] for u in users}))} "
                "will be released and re-opened by cld.[/dim]")
        return m


def _ask_mountpoint(g, taken, required):
    """Mount point under /mnt /srv /data /opt that is free and empty. None = skip."""
    suggest = _free_mountpoint(g, taken)
    while True:
        mp = prompt_str("  Mount point" + ("" if required else " (Enter = do not mount)"),
                        default=suggest if required else None, required=False)
        if not mp:
            if required:
                continue
            return None
        mp = mp.rstrip("/")
        if not _MP_RE.fullmatch(mp) or ".." in mp:
            warn("must be a path under /mnt, /srv, /data or /opt (e.g. /mnt/data1)")
            continue
        if _overlaps(mp, taken):
            warn(f"{mp} overlaps a folder already in this plan")
            continue
        rc, _ = g.run(f"findmnt -rno TARGET {_q(mp)}")
        if rc == 0:
            warn(f"{mp} is already a mount point in the guest")
            continue
        _, n = g.run(f"[ -e {_q(mp)} ] && ls -A {_q(mp)} | wc -l || echo 0")
        if n.strip() != "0":
            warn(f"{mp} exists and is not empty; mounting would hide its files")
            continue
        return mp


def _ask_access(g):
    pick = choose("  Who may write to it", ["root only", "everyone (1777, sticky "
                  "like /tmp)", "one user (owner)"], default_index=0)
    if pick.startswith("everyone"):
        return {"chmod": "1777"}
    if pick.startswith("one user"):
        while True:
            u = prompt_str("  User name in the guest", required=True)
            rc, _ = g.run(f"id -u {_q(u)}")
            if rc == 0 and re.fullmatch(r"[a-z_][a-z0-9_.-]*", u):
                return {"chown": u}
            warn(f"no user '{u}' in the guest")
    return {}


def _ask_new_fs(g, taken, label, cap):
    fstype = choose("  Filesystem", ["ext4", "xfs"], default_index=0)
    use = choose("  Use it for", ["a new, empty folder (under /mnt /srv /data /opt)",
                                  "an existing folder, moved onto it (/home/<user>, "
                                  "/var/log, /var/lib/<name>, /tmp, ...)"],
                 default_index=0)
    if use.startswith("an existing"):
        mv = _ask_move(g, taken, cap)
        if mv:
            taken.add(mv["mp"])
            # A moved folder MUST come back after a reboot: fstab is not optional.
            return dict(mv, mkfs=fstype, label=label, mode="rw", fstab=True, cap=cap)
        out("[dim]  No folder picked; it gets a new, empty folder instead.[/dim]")
    mp = _ask_mountpoint(g, taken, required=True)
    taken.add(mp)
    m = {"mkfs": fstype, "label": label, "mp": mp, "mode": "rw"}
    m.update(_ask_access(g))
    m["fstab"] = confirm("  Mount it again after every reboot (fstab)?", default=True)
    return m


def _ask_layout(g, info, taken):
    """Whole disk or partitions -> (layout, mounts). layout: None (whole disk,
    no partition table) or a list of sizes in GiB, the last None = the rest."""
    vid, size_g = info["volume_id"], info["size"] / 1024 ** 3
    pick = choose(f"Layout of this {size_g:.0f} GiB disk",
                  ["whole disk: one filesystem, no partition table",
                   "split it into partitions (GPT)"])
    if pick.startswith("whole"):
        out("[bold]Whole disk[/bold]")
        return None, [dict(_ask_new_fs(g, taken, f"data-{vid[:7]}", info["size"]),
                           src=info["byid"])]
    sizes, left = [], int(size_g) - 1          # GPT headers + alignment: keep 1 GiB
    while len(sizes) < _MAX_PARTS - 1 and left >= 1:
        n = len(sizes) + 1
        sz = prompt_int(f"Partition {n} size in GiB ({left} GiB left; 0 = all the rest)",
                        minimum=0, maximum=left)
        if sz == 0 or sz == left:
            break
        sizes.append(sz)
        left -= sz
    sizes.append(None)
    if len(sizes) == 1:
        out("[dim]One partition over the whole disk.[/dim]")
    mounts = []
    for n, sz in enumerate(sizes, 1):
        out(f"[bold]Partition {n}[/bold] ({f'{sz} GiB' if sz else 'rest of the disk'})")
        cap = (sz or left) * 1024 ** 3
        mounts.append(dict(_ask_new_fs(g, taken, f"d{vid[:7]}p{n}", cap),
                           src=f"{info['byid']}-part{n}", part=n))
    return sizes, mounts


def _ask_existing(g, info, taken):
    """Keep the data: which existing filesystem mounts where. -> mounts."""
    regions = [p for p in info["parts"] if p["fstype"]] or (
        [{"name": os.path.basename(info["dev"]), "path": info["byid"],
          "fstype": info["fstype"], "label": info.get("label", ""),
          "size": info["size"]}] if info.get("fstype") else [])
    mounts = []
    for p in regions:
        if p["fstype"] in ("swap", "LVM2_member", "crypto_LUKS"):
            out(f"[dim]{p['name']}: {p['fstype']} -- not mountable here, skipped[/dim]")
            continue
        out(f"[bold]{p['name']}[/bold]: {p['fstype']}, {_gb(p['size'])}"
            + (f", label {p['label']}" if p["label"] else ""))
        mp = _ask_mountpoint(g, taken, required=False)
        if not mp:
            continue
        taken.add(mp)
        mode = choose("  Mount mode", ["ro (read-only; safe for unvetted data)",
                                       "rw (read-write)"], default_index=0)[:2]
        m = {"src": p["path"], "fstype": p["fstype"], "mp": mp, "mode": mode,
             "mkfs": None}
        if mode == "rw":
            m.update(_ask_access(g))
        m["fstab"] = confirm("  Mount it again after every reboot (fstab)?", default=True)
        mounts.append(m)
    if not regions:
        warn("no mountable filesystem found on this disk.")
    return mounts


def plan_disk(g, info, volume, wipe_ok, server, taken):
    """Interview for one inspected disk. -> plan dict, or None (leave it alone)."""
    header(f"Integrate {getattr(volume, 'name', '') or '(unnamed)'} ({volume.id})")
    show_disk(info)
    v = info["verdict"]
    if v == "missing":
        err(f"the disk did not appear in the guest as {info['byid']} within a minute. "
            "Nothing changed inside the VM.")
        return None
    if v == "root":
        err(f"{info['dev']} is the VM's own root disk; refusing.")
        return None
    if v == "mounted":
        warn(f"already mounted at {', '.join(info.get('mounted', []))}; leaving it.")
        return None

    wipe = wipe_ok
    if v == "clash" and not wipe:
        wipe = confirm_wipe(volume, "This disk carries labels/UUIDs that "
                            f"{server.name} boots by. Mounting it as is is not "
                            "possible, and it must not stay attached over a reboot.")
        if not wipe:
            _clash_left(server, volume)
            return None
    elif v == "has-data" and not wipe:
        pick = choose("This disk holds data. What should happen",
                      ["keep the data: mount existing filesystems",
                       "wipe it and lay it out new (destroys the data)",
                       "leave it unmounted"], default_index=0)
        if pick.startswith("leave"):
            return None
        if pick.startswith("wipe"):
            wipe = confirm_wipe(volume, "Re-partitioning destroys everything on it.")
            if not wipe:
                return None
        else:
            mounts = _ask_existing(g, info, taken)
            return {"info": info, "volume": volume, "wipe": False, "layout": "keep",
                    "mounts": mounts} if mounts else None

    if wipe:
        out(f"[red]The disk will be wiped before it is laid out.[/red]")
    layout, mounts = _ask_layout(g, info, taken)
    return {"info": info, "volume": volume, "wipe": wipe, "layout": layout,
            "mounts": mounts}


def _clash_left(server, volume):
    err(f"{volume.id} is attached to {server.name} and shares its boot "
        f"identifiers. Do NOT reboot {server.name} until it is detached:")
    out(f"  openstack server remove volume {server.id} {volume.id}", wrap=False)


def show_plan(plans, server, g):
    header(f"Plan inside {server.name} (via {g.kind})")
    rows = []
    for p in plans:
        info, dev = p["info"], os.path.basename(p["info"]["dev"])
        what = ("wipe, then " if p["wipe"] else "") + (
            "keep the data" if p["layout"] == "keep" else
            "one filesystem on the whole disk" if p["layout"] is None else
            f"GPT, {len(p['layout'])} partition(s)")
        out(f"  {p['volume'].id}  [bold]{dev}[/bold]: {what}")
        for m in p["mounts"]:
            src = (os.path.basename(m["src"]) if p["layout"] == "keep" and
                   "-part" not in m["src"] else
                   f"{dev}{m['part']}" if m.get("part") else
                   next((x["name"] for x in info["parts"] if x["path"] == m["src"]), dev))
            fs = f"new {m['mkfs']}" if m.get("mkfs") else m.get("fstype", "?")
            who = (f"all (1777)" if m.get("chmod") else
                   m["chown"] if m.get("chown") else "root")
            mp = m["mp"]
            if m.get("move") == "tmp":
                mp += " (next reboot)"
            elif m.get("move"):
                mp += f" (move {_gb(m['used'])})"
                who = "as before"
            rows.append([src, fs, mp, m["mode"], who,
                         "yes" if m["fstab"] else "no"])
    render_table("Mounts", ["device", "filesystem", "mount point", "mode", "writers",
                            "fstab"], rows)
    out("[dim]fstab lines use /dev/disk/by-id/... + nofail; /etc/fstab is backed up "
        "first and verified after.[/dim]")


# --------------------------------------------------------------------------- #
# Apply
# --------------------------------------------------------------------------- #
def _sfdisk_script(sizes):
    lines = ["label: gpt"]
    for s in sizes:
        lines.append(f"size={s}GiB, type={LINUX_GUID}" if s else f"type={LINUX_GUID}")
    return "\n".join(lines) + "\n"


def _mount_opts(fstype, mode, mp=None):
    if mode == "ro":
        return {"ext4": "ro,noload", "xfs": "ro,norecovery"}.get(fstype, "ro")
    return "rw,nosuid,nodev" if mp == "/tmp" else "rw"


def _precheck(g, plans, protected, rootdev):
    """Everything re-read right before the first change. -> list of problems."""
    problems = []
    for p in plans:
        info, vid = p["info"], p["volume"].id
        rc, dev = g.run(f"readlink -f {byid(vid)}")
        if rc != 0 or dev != info["dev"]:
            problems.append(f"{vid}: device moved ({info['dev']} -> {dev})")
            continue
        if os.path.basename(dev) == rootdev:
            problems.append(f"{vid}: {dev} is the root disk")
        _, mnt = g.run(f"lsblk -nro MOUNTPOINT {dev} | grep -v '^$'")
        if mnt.strip():
            problems.append(f"{vid}: {dev} is mounted at {mnt.strip()}")
        if not p["wipe"]:
            _, ex = g.run(f"blkid -o export {dev} {dev}?* 2>/dev/null")
            clash = {f"{k}={d[k]}" for d in _blkid_ids(ex)
                     for k in ("LABEL", "UUID", "PARTUUID") if k in d} & protected
            if clash:
                problems.append(f"{vid}: boot identifier clash {sorted(clash)}")
            if p["layout"] != "keep":
                _, sig = g.run(f"wipefs -n {dev}; lsblk -nro NAME {dev} | tail -n +2")
                if sig.strip():
                    problems.append(f"{vid}: no longer blank")
        for m in p["mounts"]:
            if m.get("move"):
                prob, _ = _move_problem(g, m["mp"], m["cap"])
                if prob:
                    problems.append(f"{m['mp']} {prob}")
                elif m["move"] != "tmp":
                    _, blockers = _users(g, m["mp"], m["move"])
                    if blockers:
                        problems.append(f"{m['mp']} is in use by: "
                                        f"{'; '.join(blockers[:8])} -- stop them first")
                continue
            rc, _ = g.run(f"findmnt -rno TARGET {_q(m['mp'])}")
            _, n = g.run(f"[ -e {_q(m['mp'])} ] && ls -A {_q(m['mp'])} | wc -l || echo 0")
            if rc == 0 or n.strip() != "0":
                problems.append(f"{m['mp']} is in use or not empty")
    return problems


def _step(g, server, vid, what, cmd, timeout=120):
    rc, txt = g.run(cmd, timeout=timeout)
    ok = rc == 0
    out(f"  {what}: " + ("[green]OK[/green]" if ok else f"[red]FAILED[/red] {txt}"))
    (audit.audit if ok else audit.warn)(f"guest.{what.split()[0]}", id=vid,
                                         server=server.id, detail=what,
                                         **({} if ok else {"error": txt[-300:]}))
    return ok


def _apply_one(g, server, p, fstab_state):
    info, vid, dev = p["info"], p["volume"].id, p["info"]["dev"]
    out(f"[bold]{vid}[/bold] ({dev})")
    if p["wipe"]:
        _, dev2 = g.run(f"readlink -f {byid(vid)}")      # re-resolve right before
        if dev2 != dev:
            err(f"device moved {dev} -> {dev2}; not wiping")
            return False
        if not _step(g, server, vid, "wipe signatures",
                     f"for p in {dev}?*; do [ -b $p ] || continue; wipefs -a $p "
                     f">/dev/null || exit 1; done; wipefs -a {dev} >/dev/null && "
                     f"blockdev --rereadpt {dev} && udevadm settle"):
            return False
        _, left = g.run(f"lsblk -nro NAME {dev} | tail -n +2; wipefs -n {dev}")
        if left.strip():
            err(f"disk not blank after the wipe:\n{left}")
            return False
    if isinstance(p["layout"], list):
        if not _step(g, server, vid, f"partition GPT {len(p['layout'])}x",
                     f"printf %s {_q(_sfdisk_script(p['layout']))} | sfdisk -q {dev} "
                     f"&& udevadm settle"):
            return False
    for m in p["mounts"]:
        if m.get("mkfs"):
            rc, _ = g.run(f"for i in $(seq 20); do [ -b {_q(m['src'])} ] && break; "
                          f"sleep 1; done; [ -b {_q(m['src'])} ]", timeout=60)
            if rc != 0:
                err(f"{m['src']} did not appear")
                return False
            flag = "-F" if m["mkfs"] == "ext4" else "-f"
            if not _step(g, server, vid, f"mkfs.{m['mkfs']} {m['src'].rsplit('/', 1)[-1]}",
                         f"mkfs.{m['mkfs']} -q {flag} -L {_q(m['label'])} {_q(m['src'])} "
                         f"&& udevadm settle", timeout=900):
                return False
            m["fstype"] = m["mkfs"]
        opts = _mount_opts(m["fstype"], m["mode"], m["mp"])
        if m.get("move"):
            if not _move_in(g, server, vid, m, opts):
                return False
        elif not _step(g, server, vid, f"mount {m['mp']} ({opts})",
                       f"mkdir -p {_q(m['mp'])} && mount -o {opts} {_q(m['src'])} "
                       f"{_q(m['mp'])}"):
            return False
        if m.get("chmod") and not m.get("move"):
            _step(g, server, vid, f"chmod {m['chmod']} {m['mp']}",
                  f"chmod {m['chmod']} {_q(m['mp'])}")
        if m.get("chown"):
            _step(g, server, vid, f"chown {m['chown']} {m['mp']}",
                  f"chown {_q(m['chown'])}: {_q(m['mp'])}")
        if m["fstab"]:
            if not fstab_state.get("backup"):
                ts = g.run("date +%Y%m%d-%H%M%S")[1].strip()
                bak = f"/etc/fstab.bak-cld-{ts}"
                if not _step(g, server, vid, f"backup /etc/fstab -> {bak}",
                             f"cp -a /etc/fstab {bak}"):
                    return False
                fstab_state["backup"] = bak
            passno = "2" if m["fstype"] == "ext4" else "0"
            line = (f"{m['src']} {m['mp']} {m['fstype']} "
                    f"{opts},nofail,x-systemd.device-timeout=10s 0 {passno}")
            if not _step(g, server, vid, f"fstab + {m['mp']}",
                         f"grep -qF {_q(m['src'] + ' ')} /etc/fstab || "
                         f"echo {_q(line)} >> /etc/fstab"):
                return False
    return True


def _count(d):
    """'<entries> <bytes in regular files>' below d, lost+found of a new fs left out."""
    return (f"find {d} -xdev -mindepth 1 -path {d}/lost+found -prune -o -printf "
            f"'%y %s\\n' | awk '{{n++; if ($1 == \"f\") b += $2}} END {{print n+0, b+0}}'")


def _copy(g, server, vid, m, tmp):
    """Copy the folder onto the new filesystem and compare. -> True if equal."""
    src, q = m["mp"], _q(m["mp"])
    for attempt in (1, 2):
        if not _step(g, server, vid, f"copy {src} ({_gb(m['used'])})",
                     f"if command -v rsync >/dev/null; then rsync -aHAXx --numeric-ids "
                     f"{q}/ {tmp}/; else cp -ax {q}/. {tmp}/; fi", timeout=6 * 3600):
            return False
        _, c = g.run(f"{_count(q)}; {_count(tmp)}", timeout=1800)
        a, b = (c.splitlines() + ["", ""])[:2]
        if a and a == b:
            out(f"  compare: [green]OK[/green] ({a.split()[0]} entries)")
            return True
        try:
            (na, ba), (nb, bb) = (map(int, x.split()) for x in (a, b))
        except ValueError:
            break
        # /var/log: rsyslog may append a line between copy and compare.
        if attempt == 2 and m["move"] == "log" and na == nb and 0 <= ba - bb < 1 << 20:
            warn(f"  compare: {ba - bb} bytes of fresh log lines not copied; accepted")
            return True
    err(f"the copy differs from {src} (source: {a or '?'}, copy: {b or '?'} "
        "entries/bytes); not switching")
    audit.warn("guest.copy.mismatch", id=vid, server=server.id, folder=src)
    return False


def _move_in(g, server, vid, m, opts):
    """Copy the folder onto the new filesystem (mounted at a temporary place),
    compare, rename the folder to <folder>.cld-old-<time>, mount the new one in its
    place. The old copy is never deleted. /tmp: only prepared, switches at reboot."""
    p, q, dev = m["mp"], _q(m["mp"]), _q(m["src"])
    tmp = _q(f"/mnt/.cld-move-{m['label']}")
    if m["move"] == "tmp":
        return _step(g, server, vid, "prepare /tmp (1777, next reboot)",
                     f"mkdir -p {tmp} && mount {dev} {tmp} && chmod 1777 {tmp}; "
                     f"rc=$?; umount {tmp} 2>/dev/null; rmdir {tmp}; exit $rc")
    users, blockers = _users(g, p, m["move"])
    if blockers:
        err(f"{p} is in use by: {'; '.join(blockers[:8])}. Stop them and re-run; the "
            "new filesystem stays unused, the folder untouched.")
        audit.warn("guest.move.blocked", id=vid, server=server.id, folder=p,
                   users="; ".join(blockers[:8]))
        return False
    if not _step(g, server, vid, f"mount {m['src'].rsplit('/', 1)[-1]} at {tmp} (temp)",
                 f"mkdir -p {tmp} && mount {dev} {tmp}"):
        return False
    released = False
    if any(u.endswith(" systemd-journal") for u in users):
        if not _step(g, server, vid, "journal relinquish /var/log",
                     "journalctl --relinquish-var"):
            g.run(f"umount {tmp} && rmdir {tmp}")
            return False
        released = True

    def undo_release():
        if released:
            _step(g, server, vid, "journal flush to /var/log", "journalctl --flush")

    if not _copy(g, server, vid, m, tmp):
        g.run(f"umount {tmp} && rmdir {tmp}")
        undo_release()
        return False
    g.run(f"umount {tmp} && rmdir {tmp}")
    old = f"{p}.cld-old-{g.run('date +%Y%m%d-%H%M%S')[1].strip()}"
    qo = _q(old)
    # One shell: rename, recreate with the same owner/mode, mount. Any failure puts
    # the original folder back.
    if not _step(g, server, vid, f"move {p} -> new disk (old: {old})",
                 f"mv {q} {qo} && mkdir {q} && chown --reference={qo} {q} && "
                 f"chmod --reference={qo} {q} && mount -o {opts} {dev} {q} || "
                 f"{{ rmdir {q} 2>/dev/null; [ -e {q} ] || mv {qo} {q}; exit 1; }}"):
        undo_release()
        return False
    m["old"] = old
    g.run(f"command -v selinuxenabled >/dev/null && selinuxenabled && restorecon -R {q}",
          timeout=600)
    undo_release()
    if any(u.endswith(" rsyslogd") for u in users):
        _step(g, server, vid, "rsyslog reopen (HUP)", "pkill -HUP -x rsyslogd")
    return True


def apply_plans(g, server, plans, protected, rootdev):
    """-> True if every step succeeded. Asks once (default No) before changing."""
    problems = _precheck(g, plans, protected, rootdev)
    if problems:
        for pr in problems:
            err(pr)
        out("Nothing was changed inside the VM.")
        return False
    if not confirm(f"Apply this plan inside {server.name}?", default=False):
        out("Nothing was changed inside the VM; the disk(s) stay attached, unmounted.")
        return False

    fstab_state, ok = {}, True
    header(f"Applying inside {server.name}")
    try:
        for p in plans:
            if not _apply_one(g, server, p, fstab_state):
                ok = False
                err("stopped at the failed step; later steps and disks were not run.")
                break
        if fstab_state.get("backup"):
            rc, txt = g.run("findmnt --verify --tab-file /etc/fstab")
            if rc != 0:
                g.run(f"cp -a {fstab_state['backup']} /etc/fstab")
                err(f"fstab failed verification:\n{txt}\nrestored "
                    f"{fstab_state['backup']} (mounts stay up until the next reboot)")
                audit.warn("guest.fstab.restored", server=server.id,
                           backup=fstab_state["backup"])
                ok = False
            else:
                out(f"  findmnt --verify: [green]OK[/green]  (backup "
                    f"{fstab_state['backup']})")
                # systemd generates mount units from fstab; keep its view current.
                _step(g, server, "-", "daemon-reload (systemd reads the new fstab)",
                      "command -v systemctl >/dev/null || exit 0; systemctl daemon-reload")
        _fix_label_links(g, rootdev)
    except GuestError as e:
        err(f"lost access to the guest mid-way ({e}). The VM's disk state after the "
            "last OK step above is UNKNOWN to cld; check it before re-running.")
        audit.warn("guest.lost", server=server.id, error=str(e))
        return False
    _verify(g, plans)
    for m in (m for p in plans for m in p["mounts"]):
        if m.get("old"):
            out(f"Old copy of {m['mp']}: [bold]{m['old']}[/bold] ({_gb(m['used'])}, on "
                "the VM's own disk). Check the moved folder, then delete it yourself: "
                f"rm -rf {_q(m['old'])}", wrap=False)
        elif m.get("move") == "tmp" and ok:
            out("/tmp switches to the new disk at the next reboot (fstab entry added).")
    return ok


def _fix_label_links(g, rootdev):
    """After old labels vanished, /dev/disk/by-label/* must point at the VM's own
    disk again (udev keeps the last disk it saw)."""
    _, links = g.run("for l in /dev/disk/by-label/*; do echo $l $(readlink -f $l); done")
    stray = [ln for ln in links.splitlines() if len(ln.split()) == 2
             and not os.path.basename(ln.split()[1]).startswith(rootdev)
             and not re.match(r"(data-|d[0-9a-f]{7}p)", os.path.basename(ln.split()[0]))]
    if stray:
        g.run(f"udevadm trigger --action=change /dev/{rootdev}* && udevadm settle")


def _verify(g, plans):
    header("Result")
    mps = " ".join(_q(m["mp"]) for p in plans for m in p["mounts"])
    devs = " ".join(_q(p["info"]["dev"]) for p in plans)
    out(g.run(f"lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT {devs}; echo; df -h {mps}")[1],
        wrap=False)
