"""attachstorage: create a Cinder data volume and attach it to an EXISTING server.

Decoupling storage from VM creation is the structural fix for rc3 issue #112:
this command operates on a pre-existing server, so its rollback can only ever
delete the dangling volume -- it never touches the server.

After the attach it goes INTO the guest (cld.guest: qemu-guest-agent, SSH as a
fallback) and lays the disk out with the operator (cld.guestdisk). An old root
disk is only attached when the operator has agreed, by typing its ID, that it is
wiped right after: its cloud-image labels would otherwise hijack the VM's boot.
"""

import time

from cld import audit, rbdpeek
from cld.cloud import connect, safe_list
from cld.guest import connect_guest, GuestError
from cld.guestdisk import (boot_ids, root_disk, inspect_disk, plan_disk, show_plan,
                           apply_plans, confirm_wipe)
from cld.inventory import server_az
from cld.steps import current_project
from cld.ui import (out, header, warn, err, render_table, choose, confirm,
                    confirm_destructive, Abort)
from cld.volume import show_capacity, prompt_volume_spec


def _resolve_server(conn, project_id, server_arg):
    """Fetch the target server by ID, or let the user choose one."""
    if server_arg:
        srv = conn.compute.find_server(server_arg, ignore_missing=True)
        if srv is None:
            err(f"server with ID '{server_arg}' not found in this project")
            return None
        return conn.compute.get_server(srv.id)

    servers = safe_list(conn.compute.servers, details=True)
    if not servers:
        err("no servers found in this project to attach storage to")
        return None
    rows = [[s.name, getattr(s, "status", "?"), s.id] for s in servers]
    render_table("Servers in this project", ["name", "status", "id"], rows)
    return choose("Select target server", servers,
                  label=lambda s: f"{s.name}  [{getattr(s, 'status', '?')}]")


def attach_storage(cloud, server_arg=None, size=None, type_name=None,
                   disks=None, dry_run=False, integrate=True):
    # --disk attaches an existing volume that already has its own size/type, so
    # combining it with --size/--type is contradictory -- refuse up front.
    disks = list(dict.fromkeys(disks or []))
    if disks and (size is not None or type_name is not None):
        err("--size/--type cannot be combined with --disk (an existing volume "
            "already has its own size and type).")
        return None

    conn = connect(cloud)
    project_id, _ = current_project(conn)
    audit.audit("attachstorage.start", cloud=cloud, project=project_id,
                user=getattr(conn, "current_user_id", None),
                disk=",".join(disks) or None)

    server = _resolve_server(conn, project_id, server_arg)
    if server is None:
        return None
    out(f"Target server: [bold]{server.name}[/bold] [dim]({server.id})[/dim]")

    # --disk: attach EXISTING volumes instead of creating a new one.
    if disks:
        return _attach_existing(conn, server, disks, dry_run, integrate)

    header("Data volume")
    pool_free, quota_gb = show_capacity(conn, project_id)
    spec = prompt_volume_spec(conn, pool_free, quota_gb,
                              size=size, type_name=type_name)
    if spec is None:
        return None

    render_table("Volume to create + attach", ["field", "value"],
                 [["server", f"{server.name} ({server.id})"],
                  ["size", f"{spec['size']} GB"],
                  ["type", spec.get("type") or "(default)"]])

    if dry_run:
        out()
        out("[yellow]--dry-run: no volume created or attached.[/yellow]")
        if integrate:
            connect_guest(conn, server, ask_ssh=False)
        return None

    if not confirm(f"Create and attach a {spec['size']} GB volume to "
                   f"{server.name}?", default=False):
        out("Aborted; nothing created.")
        return None

    volume = _create_and_attach(conn, server, spec)
    if volume is not None:
        _after_attach(conn, server, [volume], integrate)
    return volume


def _attached_servers(conn, volume):
    """Readable names of the server(s) a volume is currently attached to."""
    names = []
    for a in (getattr(volume, "attachments", None) or []):
        sid = a.get("server_id")
        if not sid:
            continue
        try:
            s = conn.compute.get_server(sid)
            names.append(f"{s.name} ({sid})")
        except Exception:  # noqa: BLE001 - fall back to the raw id
            names.append(sid)
    return ", ".join(names) or "(unknown)"


def _guest_device(volume):
    """The exact in-guest path of this volume, independent of attach order.

    Nova's libvirt driver sets the virtio disk serial to the Cinder volume ID, and
    virtio-blk truncates serials to 20 chars, so udev links the disk as
    /dev/disk/by-id/virtio-<first 20 chars of the volume ID>. Nova's reported
    /dev/vdX is only the name it *requested*; the guest kernel names disks in
    discovery order and can disagree (and the name can change across reboots).
    """
    return f"/dev/disk/by-id/virtio-{volume.id[:20]}"


def _attached_device(conn, server, volume, wait=30):
    """Device name Nova reports for this volume on `server` (e.g. /dev/vdb), or
    None. The attach call returns before Cinder records the attachment, so poll
    (read-only) for up to `wait` seconds instead of reading it once, too early."""
    deadline = time.monotonic() + wait
    while True:
        try:
            v = conn.block_storage.get_volume(volume.id)
        except Exception:  # noqa: BLE001
            v = volume
        for a in (getattr(v, "attachments", None) or []):
            if a.get("server_id") == server.id and a.get("device"):
                return a.get("device")
        if time.monotonic() >= deadline:
            return None
        time.sleep(2)


def _is_image_volume(volume):
    return bool(getattr(volume, "is_bootable", False)
                or getattr(volume, "volume_image_metadata", None))


def _print_mount_help(conn, server, volume):
    """Tell the operator how to mount the freshly-attached volume INSIDE the VM.

    The fallback when cld cannot get into the guest (no guest agent, no SSH) or
    the operator stopped the in-guest step: the exact device + safe,
    copy-pasteable steps instead of guessing.
    Every command uses the by-id path: it names THIS volume, where a /dev/vdX
    guess could name another disk -- and mkfs on the wrong one is unrecoverable.
    """
    dev = _guest_device(volume)
    nova_dev = _attached_device(conn, server, volume)
    mp = "/mnt/data"
    out()
    header("Mount it inside the VM")
    out(f"Attached to {server.name} as [bold]{dev}[/bold]")
    out(f"[dim](Nova reports {nova_dev or 'no device name yet'}; the guest's /dev/vdX "
        f"can differ, so use the by-id path -- it is always this volume. If it is "
        f"missing, find it with: ls -l /dev/disk/by-id/ | grep {volume.id[:20]})[/dim]")
    out("cld did not set it up inside the VM; do this there (as root):")
    out()
    out("  [dim]# 1. existing partitions/filesystem? (wipefs: no output = blank)[/dim]")
    out(f"  [cyan]lsblk -f {dev}[/cyan]", wrap=False)
    out(f"  [cyan]sudo wipefs -n {dev}[/cyan]", wrap=False)
    out("  [dim]# 2. ONLY if blank -- mkfs ERASES the disk[/dim]")
    out(f"  [cyan]sudo mkfs.ext4 -L data-{volume.id[:8]} {dev}[/cyan]", wrap=False)
    out("  [dim]# 3. mount, persist, verify[/dim]")
    out(f"  [cyan]sudo mkdir -p {mp}[/cyan]", wrap=False)
    out(f"  [cyan]sudo mount {dev} {mp}[/cyan]", wrap=False)
    out(f"  [cyan]echo \"{dev} {mp} ext4 defaults,nofail,x-systemd.device-timeout=10s "
        f"0 2\" | sudo tee -a /etc/fstab[/cyan]", wrap=False)
    out("  [cyan]sudo findmnt --verify[/cyan]", wrap=False)
    out()
    out("[dim]fstab uses the by-id path, not UUID= (cloned image volumes share "
        "filesystem UUIDs); nofail keeps a later detach from blocking boot.[/dim]")
    warn("Only run mkfs if the disk is blank; it destroys existing data.")
    if _is_image_volume(volume):
        _warn_boot_label_clash(server)


def _warn_boot_label_clash(server):
    """An image/root volume carries the cloud image's filesystem labels
    (cloudimg-rootfs, UEFI, BOOT) -- the very labels Ubuntu cloud guests boot and
    mount by (root=LABEL=cloudimg-rootfs, fstab LABEL=UEFI/BOOT). udev points
    /dev/disk/by-label/* at the disk it saw LAST, so after this attach the VM's
    next reboot can mount THIS volume as / or /boot. cld cannot read guest labels,
    so it warns on every image volume; seen live on rc3 Mailcow1, 2026-09-21."""
    out()
    warn(f"this is an image/root volume: it very likely carries the labels "
         f"'cloudimg-rootfs' / 'UEFI' / 'BOOT' that {server.name} itself boots by. "
         f"Do NOT reboot {server.name} until that is resolved -- it may come up on "
         f"THIS disk's root filesystem.")
    out("  Check inside the VM:  [cyan]ls -l /dev/disk/by-label/[/cyan]  "
        "[dim]# those links must point at the VM's own disk[/dim]")
    out("  Resolve by reformatting this volume (if its data is disposable) or "
        "detaching it.")


_ROOT_LABELS = {"cloudimg-rootfs", "uefi", "boot", "efi", "root", "rootfs",
                "system-boot", "esp"}


def _old_root_reasons(volume, peek):
    """Why this volume looks like an old VM's root disk ([] = it does not)."""
    why = []
    if getattr(volume, "is_bootable", False):
        why.append("Cinder marks it bootable")
    meta = getattr(volume, "volume_image_metadata", None) or {}
    if meta:
        why.append(f"it was made from image '{meta.get('image_name', '?')}'")
    for r in (peek.get("regions") or []) if peek.get("found") else []:
        label = ((r.get("fs") or {}).get("label") or "").lower()
        if label in _ROOT_LABELS:
            why.append(f"{r['name']} has the boot label '{label}'")
    return why


def _show_peek(peek):
    if not peek.get("found"):
        out(f"[dim]contents not inspected: {peek.get('error') or 'no Ceph image'}[/dim]")
        return
    rows = []
    for r in peek.get("regions", []):
        fs = r.get("fs") or {}
        rows.append([r["name"], f"{r['size_bytes'] / 1024 ** 3:.1f}G",
                     fs.get("type", "?"), fs.get("label") or "-",
                     fs.get("last_mounted_on") or "-"])
    render_table(f"On the disk now ({peek.get('partition_table') or 'no partition table'};"
                 " read-only Ceph look)", ["part", "size", "filesystem", "label",
                                           "last mounted on"], rows)


def _validate_existing(conn, server, volume_id):
    """-> the volume if it may be attached to `server`, else None (with the reason).
    Refuses unless it exists, is in the same project and is `available` -- or is
    already attached to `server` alone (then only the in-guest step runs;
    `volume._cld_attached` marks it)."""
    volume = conn.block_storage.find_volume(volume_id, ignore_missing=True)
    if volume is None:
        err(f"volume '{volume_id}' not found in this project")
        return None
    vproj = getattr(volume, "project_id", None)
    sproj = getattr(server, "project_id", None)
    if vproj and sproj and vproj != sproj:
        err(f"volume {volume.id} belongs to a different project ({vproj}) than the "
            f"server ({sproj}); cross-project attach is not allowed.")
        return None
    status = getattr(volume, "status", None)
    atts = getattr(volume, "attachments", None) or []
    if (status == "in-use" and len(atts) == 1
            and atts[0].get("server_id") == server.id):
        out(f"[dim]{volume.id} is already attached to {server.name}: only the "
            "in-guest step runs for it.[/dim]")
        volume._cld_attached = True
        return volume
    if atts or status == "in-use":
        err(f"volume {volume.id} is already attached to "
            f"{_attached_servers(conn, volume)}; refusing (status={status}).")
        return None
    if status != "available":
        err(f"volume {volume.id} is not available for attach (status={status}); "
            "refusing.")
        return None
    return volume


def _attach_existing(conn, server, volume_ids, dry_run, integrate=True):
    """Attach pre-existing volumes to `server`, defensively, then integrate them.

    Each volume must exist, be in the same project and `available`. An old root
    disk (bootable / from an image / boot labels on it) is attached only if cld
    can get into the guest AND the operator typed its ID to agree that it is
    wiped right after; otherwise it is not attached at all. cld never deletes a
    pre-existing volume; on an attach failure it is left as it was.
    """
    header("Attach existing volume(s)")
    vols = [v for v in (_validate_existing(conn, server, i) for i in volume_ids) if v]
    if not vols:
        return None

    guest = connect_guest(conn, server, ask_ssh=not dry_run) if integrate else None
    wipe_ids, keep = set(), []
    saz = server_az(server)
    for v in vols:
        vname = getattr(v, "name", "") or "(unnamed)"
        header(f"{vname} ({v.id}, {getattr(v, 'size', '?')} GB)")
        peek = rbdpeek.peek(v.id)
        _show_peek(peek)
        vaz = getattr(v, "availability_zone", None)
        if vaz and saz and vaz != saz:
            out(f"[dim]volume AZ '{vaz}', server AZ '{saz}': fine unless Nova's "
                "cinder.cross_az_attach is off (it is not on rc3).[/dim]")
        why = _old_root_reasons(v, peek)
        if why:
            warn(f"old root disk: {'; '.join(why)}. Its cloud-image labels can make "
                 f"{server.name} boot from it, so cld attaches it only to wipe it.")
            if not integrate or guest is None:
                if getattr(v, "_cld_attached", False):
                    err(f"it is attached and cld cannot get into the guest to wipe it. "
                        f"Do not reboot {server.name} before checking "
                        "`ls -l /dev/disk/by-label/` inside it, or detach it.")
                else:
                    err("no way into the guest to wipe it right after the attach; "
                        "not attaching this disk.")
                continue
            if dry_run:
                out("[yellow]--dry-run: a real run asks you to type its ID to agree "
                    "to wipe it.[/yellow]")
            elif not confirm_wipe(v, "It will be wiped inside the VM right after "
                                     "the attach, then laid out as you choose."):
                if getattr(v, "_cld_attached", False):
                    warn(f"leaving {v.id} attached and untouched. Before rebooting "
                         f"{server.name}, check `ls -l /dev/disk/by-label/` inside it "
                         "points only at its own disk, or detach this volume.")
                else:
                    out(f"Not attaching {v.id}.")
                continue
            wipe_ids.add(v.id)
        keep.append(v)
    if not keep:
        out("Nothing to attach.")
        return None

    render_table("Volumes", ["volume", "size", "old root disk", "attach"],
                 [[f"{getattr(v, 'name', '') or '(unnamed)'} ({v.id})",
                   f"{getattr(v, 'size', '?')} GB",
                   "yes: wiped in the guest" if v.id in wipe_ids else "no",
                   "already attached" if getattr(v, "_cld_attached", False)
                   else f"-> {server.name}"] for v in keep])
    if dry_run:
        out()
        out("[yellow]--dry-run: nothing attached, nothing changed in the guest.[/yellow]")
        if integrate and guest is None:
            out("[dim]No guest access found: a real run would offer SSH, or print the "
                "manual steps.[/dim]")
        return None

    new = [v for v in keep if not getattr(v, "_cld_attached", False)]
    if new and not confirm(f"Attach {len(new)} volume(s) to {server.name}?",
                           default=False):
        out("Aborted; nothing attached.")
        return None
    attached = [v for v in keep
                if getattr(v, "_cld_attached", False) or _attach_one(conn, server, v)]
    if not attached:
        return None
    _after_attach(conn, server, attached, integrate, guest, wipe_ids)
    return attached


def _attach_one(conn, server, volume):
    """One Nova attach. -> True on success; on failure report VERIFIED state."""
    try:
        out(f"[bold]Attaching {volume.id}...[/bold]")
        conn.compute.create_volume_attachment(server, volume_id=volume.id)
        out(f"[green]Attached volume:[/green] {volume.id} -> {server.name}")
        audit.audit("volume.attach", id=volume.id, server=server.id, existing=True)
        return True
    except Exception as e:  # noqa: BLE001
        err(f"attach failed: {e}")
        fresh = None
        try:
            fresh = _diagnose_failure(conn, "attach", volume, e)
        finally:
            # finally: the audit line must survive anything the diagnosis does.
            audit.warn("volume.attach.failed", id=volume.id, server=server.id,
                       existing=True, phase="attach", error=str(e),
                       status_after=getattr(fresh, "status", None))
        # The guarantee cld can actually make. What Nova did to the volume is
        # reported by _recheck_volume above, from a real Cinder read.
        warn("cld did not create, modify, or delete this volume.")
        return False


def _after_attach(conn, server, volumes, integrate, guest=None, wipe_ids=()):
    """In-guest half: integrate the attached volumes, or print the manual steps.
    Anything left undone on a disk that must be wiped is reported loudly."""
    done = False
    if integrate:
        if guest is None:
            guest = connect_guest(conn, server)
        if guest is not None:
            done = _integrate(server, guest, volumes, set(wipe_ids))
    if not done:
        for v in volumes:
            _print_mount_help(conn, server, v)
        for v in volumes:
            if v.id in wipe_ids:
                err(f"{v.id} is an old root disk and was NOT wiped. Do NOT reboot "
                    f"{server.name}; detach it first:")
                out(f"  openstack server remove volume {server.id} {v.id}", wrap=False)


def _integrate(server, g, volumes, wipe_ids):
    """Inspect -> interview -> apply inside the guest. -> True when every disk
    was handled (or deliberately left alone without a pending wipe)."""
    try:
        protected, rootdev = boot_ids(g), root_disk(g)
        taken, plans = set(), []
        for v in volumes:
            info = inspect_disk(g, v.id, protected, rootdev)
            p = plan_disk(g, info, v, v.id in wipe_ids, server, taken)
            if p:
                plans.append(p)
            elif v.id in wipe_ids:
                return False
        if not plans:
            return True
        show_plan(plans, server, g)
        return apply_plans(g, server, plans, protected, rootdev)
    except Abort:
        warn("stopped; the disk(s) stay attached, nothing more changed in the guest.")
        return False
    except GuestError as e:
        err(f"lost access to the guest ({e}); nothing more changed in the guest.")
        return False


# Markers matched case-insensitively against str(e). We only ever see the client
# side: the wrapped Cinder class name is sometimes (not always) echoed in the 500
# body, and the decisive socket-level cause (reset vs refused) lives in nova-api.log
# on the controller -- so we point the operator there rather than claim certainty.
# We deliberately do NOT retry (see CLAUDE.md / #111).
_CINDER_CONN_MARKERS = ("cinderconnectionfailed",)              # confirmed #111 family
_ATTACH_500_MARKERS = ("os-volume_attachments", "unexpected api error",
                       "500: server error")                     # server-side attach error
_CLIENT_CONN_MARKERS = ("connection refused", "connection reset", "remotedisconnected",
                        "max retries", "failed to establish a new connection",
                        "connection aborted", "read timed out")  # never reached Nova


# Post-failure Cinder statuses, grouped by what they mean for the operator.
# A 500 from POST /os-volume_attachments can land on either side of Cinder
# reserving the volume, so the pre-attach snapshot proves nothing -- we re-read.
_NOVA_RESERVED = ("reserved", "attaching")      # Nova got in before failing
_MAYBE_ATTACHED = ("in-use", "detaching")       # the attach may have succeeded


def _recheck_volume(conn, volume):
    """Re-read the volume from Cinder after a failed attach and report what is
    ACTUALLY there. Returns the fresh volume, or None if it could not be read.

    Read-only (one GET), and it never raises: it runs inside an exception
    handler. An unreadable volume is reported as UNKNOWN, never as "unchanged" --
    asserting cluster state we did not read is the bug this exists to prevent.
    """
    vid = getattr(volume, "id", None)
    if conn is None or not vid:
        return None
    try:
        fresh = conn.block_storage.get_volume(vid)
    except Exception as e:  # noqa: BLE001
        fresh = None
        warn(f"could not re-read volume {vid} to verify its state ({e}). Treat its "
             f"status as UNKNOWN and check with 'openstack volume show {vid}' "
             f"before retrying.")
        return None
    if fresh is None:
        warn(f"volume {vid} could not be found on re-read; its state is UNKNOWN.")
        return None

    status = getattr(fresh, "status", None)
    attachments = getattr(fresh, "attachments", None) or []
    if status in _MAYBE_ATTACHED or attachments:
        warn(f"volume {vid} is now '{status}' -- the attach may have SUCCEEDED "
             f"despite the error. Do not re-run blindly; verify first:")
        out(f"  openstack volume show {vid}")
        out(f"  it currently shows attached to: {_attached_servers(conn, fresh)}")
    elif status in _NOVA_RESERVED:
        warn(f"volume {vid} is now '{status}' -- Nova reserved it in Cinder before "
             f"the call failed, so it is no longer 'available' and a plain re-run "
             f"will be refused. The stale attachment must be cleared by a human:")
        out(f"  openstack volume attachment list --volume {vid}")
    elif status == "error":
        warn(f"volume {vid} is now in 'error'; a re-run will be refused. Check the "
             f"cinder-volume log for this volume before doing anything else.")
    elif status == "available":
        out(f"[dim]Verified: volume {vid} is still 'available' (unchanged); a re-run "
            f"retries the attach.[/dim]")
    else:
        warn(f"volume {vid} is now '{status}' (not 'available'); a re-run will be "
             f"refused until it returns to 'available'.")
    return fresh


def _attach_checks(conn, volume):
    """The three read-only checks that settle transient #111 vs persistent,
    followed by the VERIFIED current state of the volume. Returns the fresh
    volume (or None)."""
    out("  1. nova-api.log around the failure time -- the wrapped socket error:")
    out("       reset / RemoteDisconnected / BadStatusLine  -> transient #111 (clears on retry)")
    out("       refused / timed out / DNS failure           -> cinder-api unreachable (persistent)")
    out("  2. openstack volume service list  -- are cinder-api/scheduler/volume 'up'?")
    out("  3. re-run attachstorage  -- a keep-alive race clears within a retry or two.")
    return _recheck_volume(conn, volume)


def _diagnose_failure(conn, phase, volume, e):
    """Print an honest, actionable diagnosis after a create/attach failure.

    Classifies only what is visible client-side, points at the read-only checks
    that distinguish a transient #111 keep-alive race from a persistent Cinder
    outage, and -- where Nova may have touched the volume -- re-reads it. Adds no
    retry and triggers no live operation (the re-read is a GET).

    Returns the freshly-read volume, or None when it was not read (the request
    never reached Nova, Nova rejected it outright, or the re-read failed)."""
    msg = str(e).lower()

    if phase == "attach":
        # Got an HTTP response from Nova (a 500), vs never reached Nova at all.
        got_500 = any(m in msg for m in _ATTACH_500_MARKERS)
        if any(m in msg for m in _CINDER_CONN_MARKERS):
            warn("Confirmed Nova->Cinder connection failure (rc3 #111 family), not a "
                 "rejection of the request itself.")
            out("[dim]Consistent with the transient HAProxy keep-alive race (#111), but "
                "the 500 alone does not prove it. To be sure:[/dim]")
            return _attach_checks(conn, volume)
        if got_500:
            warn("The attach call returned a 500 server error from Nova. On this "
                 "cluster that is most often the #111 Nova->Cinder connection race -- "
                 "but the 500 alone does not prove the cause.")
            return _attach_checks(conn, volume)
        if any(m in msg for m in _CLIENT_CONN_MARKERS):
            warn("Could not reach the Nova compute API at all (no HTTP response).")
            out("  - is nova-api up, and the VIP/HAProxy frontend (192.168.1.100:8774) reachable?")
            out("  - openstack server list  -- does any compute call work right now?")
            out("[dim]The request never reached Nova, so the volume is untouched.[/dim]")
            return None
        # Some other attach error (e.g. a 4xx rejection) -- show it plainly.
        warn("Attach was rejected by Nova (not a connection failure). Inspect the "
             "message above and nova-api.log for the specific reason.")
        return _recheck_volume(conn, volume)

    if phase in ("create", "wait"):
        detail = ("the volume never reached 'available' (stuck creating)"
                  if phase == "wait" else "the volume create call failed")
        warn(f"Failure on the operator->Cinder path: {detail}, before any attach.")
        out("  - openstack volume service list  -- is cinder-volume 'up'?")
        out("  - cld list capacity  -- can this host reach the Cinder API, and is there room?")
        out("  - check the volume type is valid and quota/pool capacity allow the size.")
        return None

    warn("Unexpected failure. Inspect nova-api.log and the cinder-api/volume logs "
         "around the failure time for the underlying cause.")
    return None


def _create_and_attach(conn, server, spec):
    volume = None
    phase = "create"
    try:
        out("[bold]Creating data volume...[/bold]")
        vargs = {"name": f"{server.name}-data", "size": spec["size"]}
        if spec.get("type"):
            vargs["volume_type"] = spec["type"]
        volume = conn.block_storage.create_volume(**vargs)
        audit.audit("volume.create", id=volume.id, size=spec["size"],
                    type=spec.get("type"))
        phase = "wait"
        volume = conn.block_storage.wait_for_status(volume, status="available",
                                                    wait=300)
        phase = "attach"
        conn.compute.create_volume_attachment(server, volume_id=volume.id)
        out(f"[green]Attached volume:[/green] {volume.id} ({spec['size']} GB) "
            f"-> {server.name}")
        audit.audit("volume.attach", id=volume.id, server=server.id)
    except Exception as e:  # noqa: BLE001
        err(f"volume create/attach failed: {e}")
        fresh = None
        try:
            fresh = _diagnose_failure(conn, phase, volume, e)
        finally:
            # finally: the audit line must survive anything the diagnosis does.
            audit.warn("volume.failed", id=getattr(volume, "id", None),
                       server=server.id, phase=phase, error=str(e),
                       status_after=getattr(fresh, "status", None))
        _offer_volume_rollback(conn, volume, fresh)
        return None

    out()
    out(f"[bold green]Done.[/bold green] Volume {volume.id} attached to "
        f"{server.name}")
    return volume


def _offer_volume_rollback(conn, volume, fresh=None):
    """Offer to delete the dangling volume. Defaults to KEEPING it, and NEVER
    touches the server (it pre-existed this command). Structural #112 guard.
    Quitting (q/Ctrl-D) keeps the volume too -- see confirm_destructive (#116).

    `fresh` is the volume as re-read from Cinder after the failure (see
    _recheck_volume), or None if it could not be read. A volume Nova may already
    have reserved or attached is never offered for deletion at all; an unverified
    one still prompts, and that prompt still defaults to keeping it."""
    if volume is None:
        return
    status = getattr(fresh, "status", None)
    attachments = getattr(fresh, "attachments", None) or []
    if status in _MAYBE_ATTACHED or attachments:
        warn(f"not offering to delete this volume: Cinder shows it '{status}', so the "
             f"attach may have succeeded. Detach it by hand if it is unwanted.")
        return
    if status in _NOVA_RESERVED:
        warn(f"not offering to delete this volume: Cinder shows it '{status}', so Nova "
             f"holds an attachment on it. That has to be cleared by hand first.")
        return
    if not confirm_destructive("Delete the volume that failed to attach?",
                               default=False):
        warn("Leaving the volume in place for inspection.")
        return
    try:
        conn.block_storage.delete_volume(volume, ignore_missing=True)
        out("[dim]deleted volume[/dim]")
        audit.audit("volume.delete", id=getattr(volume, "id", None),
                    reason="rollback")
    except Exception as e:  # noqa: BLE001
        warn(f"could not delete volume: {e}")
