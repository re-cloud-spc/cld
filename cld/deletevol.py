"""deletevolume: permanently delete ONE unattached Cinder volume, defensively.

The only delete path in cld that removes something the operator did not just
create. So the flow is built to make a wrong delete hard:

  1. Hard refusals (no override): not a full UUID, other project, attached
     anywhere (Cinder attachments, a Nova server that still lists it, or an RBD
     watcher holding the image open), any non-final status (reserved,
     attaching, error_deleting, ...), or Cinder snapshots (cld never cascades).
  2. A report: Cinder facts, age, provenance, cld audit history, and what is
     actually ON the volume (cld.rbdpeek: read-only raw-image inspection).
  3. The operator decides: confirm_destructive(default=False), then typing the
     first 8 characters of the volume ID. Any other answer, Enter, q or
     Ctrl-D keeps the volume.
  4. Re-read right before the call (the volume must be unchanged), delete
     without force/cascade, audit, then verify from Cinder that it is gone.

Attached volumes are never detached here: the operator is told to detach
through the normal process first, then re-run.
"""

import datetime
import glob
import os
import re
import time

from openstack import exceptions as os_exc

from cld import audit, rbdpeek
from cld.cloud import connect, safe_list
from cld.steps import current_project
from cld.ui import (out, header, warn, err, render_table, confirm_destructive,
                    _ask_text, Abort)

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
DELETABLE = ("available", "error")          # final states Cinder can delete from
_TRANSITIONAL_HINT = {
    "reserved": "Nova reserved it for an attach that never completed (a stale "
                "reservation). A human must clear it: 'openstack volume attachment "
                "list --volume {id}', then delete that attachment record.",
    "attaching": "an attach is in progress or got stuck; wait, then check "
                 "'openstack volume show {id}'.",
    "detaching": "a detach is in progress or got stuck; wait, then check "
                 "'openstack volume show {id}'.",
    "error_deleting": "a previous delete failed on the backend; check the "
                      "cinder-volume log before anything else.",
}


def _age(iso):
    """'2025-06-07T04:34:54.000000' -> ('2025-06-07 04:34', '471 days')."""
    if not iso:
        return "-", ""
    try:
        t = datetime.datetime.fromisoformat(iso.replace("Z", "")[:19])
    except ValueError:
        return iso, ""
    days = (datetime.datetime.utcnow() - t).days
    return t.strftime("%Y-%m-%d %H:%M"), f"{days} days ago"


def _hsize(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _audit_history(volume_id):
    """cld's own log lines that mention this volume (oldest first)."""
    logs = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
    lines = []
    for path in sorted(glob.glob(os.path.join(logs, "cld-*.log"))):
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines += [ln.rstrip() for ln in fh if volume_id in ln]
        except OSError:
            continue
    return lines


def _nova_holders(conn, volume_id):
    """Servers (any project) whose Nova record still lists the volume -- catches
    a Cinder/Nova mismatch where Cinder already says 'available'."""
    holders = []
    for s in safe_list(conn.compute.servers, details=True, all_projects=True):
        vols = [v.get("id") for v in (getattr(s, "attached_volumes", None) or [])]
        if volume_id in vols:
            holders.append(s)
    return holders


def _refuse_attached(conn, volume, holders):
    err(f"volume {volume.id} is attached -- cld will not delete it.")
    names = []
    for a in (getattr(volume, "attachments", None) or []):
        sid = a.get("server_id")
        try:
            s = conn.compute.get_server(sid)
            names.append((s.name, sid, getattr(s, "status", "?"), a.get("device") or "?"))
        except Exception:  # noqa: BLE001
            names.append(("?", sid, "?", a.get("device") or "?"))
    for s in holders:
        if s.id not in [n[1] for n in names]:
            names.append((s.name, s.id, getattr(s, "status", "?"), "(Nova record)"))
    if names:
        render_table("Attached to", ["server", "id", "status", "device"],
                     [list(n) for n in names])
    out("To delete it, first detach it through the normal process:")
    out("  1. inside the VM: stop whatever uses the disk, [cyan]umount[/cyan] it, and "
        "remove its line from [cyan]/etc/fstab[/cyan] (or the VM may hang/fail on boot)")
    for n in names or [("<server>", "<server-id>", "", "")]:
        out(f"  2. [cyan]openstack server remove volume {n[1]} {volume.id}[/cyan]"
            f"   [dim]# {n[0]}[/dim]")
    out("  3. wait for status 'available', then re-run "
        f"[cyan]cld deletevolume --volumeid {volume.id}[/cyan]")


def _render_report(volume, snaps, backups, clones, history, peek):
    created, created_ago = _age(getattr(volume, "created_at", None))
    updated, updated_ago = _age(getattr(volume, "updated_at", None))
    img = (getattr(volume, "volume_image_metadata", None) or {})
    iname = img.get("image_name") or "(name not recorded)"
    iid = (img.get("image_id") or "")[:8]
    src = (f"image {iname}" + (f" ({iid})" if iid else "") if img
           else f"snapshot {volume.snapshot_id}" if getattr(volume, "snapshot_id", None)
           else f"volume {volume.source_volid}" if getattr(volume, "source_volid", None)
           else "blank (created empty)")
    render_table("Volume", ["field", "value"], [
        ["id", volume.id],
        ["name", getattr(volume, "name", "") or "(unnamed)"],
        ["description", getattr(volume, "description", "") or "-"],
        ["size / type", f"{getattr(volume, 'size', '?')} GB / {getattr(volume, 'volume_type', '-')}"],
        ["status", getattr(volume, "status", "?")],
        ["bootable", "yes" if getattr(volume, "is_bootable", False) else "no"],
        ["created from", src],
        ["created", f"{created}  ({created_ago})"],
        ["last changed", f"{updated}  ({updated_ago})  [dim]Cinder record, not I/O[/dim]"],
        ["metadata", ", ".join(f"{k}={v}" for k, v in (getattr(volume, "metadata", None) or {}).items()) or "-"],
        ["snapshots", ", ".join(s.id for s in snaps) or "none"],
        ["backups", ", ".join(f"{b.id} ({getattr(b, 'status', '?')})" for b in backups) or "none"],
        ["volumes cloned from it", ", ".join(v.id for v in clones) or "none"],
    ])

    header("Contents (read-only look at the Ceph image)")
    if not peek.get("found") and not peek.get("error"):
        warn(f"no Ceph image named volume-{volume.id} exists in any pool."
             + (" For an 'error' volume this usually means creation failed before the "
                "backend image was made, so there is no data behind it."
                if getattr(volume, "status", None) == "error" else
                " An 'available' volume should have one -- treat this as unexplained."))
    elif not peek.get("found"):
        warn(f"contents could not be inspected: {peek['error']}. "
             "Decide on the metadata above only if you are sure.")
    else:
        alloc = peek.get("allocated_bytes")
        out(f"Ceph image {peek['pool']}/{peek['image']}: {_hsize(peek['size_bytes'])} "
            f"provisioned, [bold]{_hsize(alloc)} ever written[/bold]")
        out(f"  RBD timestamps: created {peek.get('rbd_created') or '-'}, last modified "
            f"{peek.get('rbd_modified') or '-'}, last accessed {peek.get('rbd_accessed') or '-'} (UTC)")
        if peek.get("parent"):
            out(f"  cloned from {peek['parent']}")
        if peek.get("children"):
            warn(f"RBD images cloned from this one: {', '.join(peek['children'])}")
        if alloc == 0:
            out("  [green]Never written: the volume holds no data at all.[/green]")
        else:
            rows = []
            for r in peek.get("regions", []):
                fs = r.get("fs") or {}
                used = (f"{_hsize(fs['used_bytes'])} of {_hsize(fs['size_bytes'])}"
                        if fs.get("size_bytes") else "")
                last = ""
                if fs.get("last_mounted"):
                    last = f"{fs['last_mounted']} on {fs.get('last_mounted_on') or '?'}"
                rows.append([r["name"], _hsize(r["size_bytes"]), fs.get("type", "?"),
                             fs.get("label") or "", used, last or "-",
                             _hsize(fs.get("lifetime_written_bytes"))
                             if fs.get("lifetime_written_bytes") else ""])
            render_table(f"Layout ({peek.get('partition_table') or 'no partition table'})",
                         ["part", "size", "filesystem", "label", "used", "last mounted (UTC)",
                          "lifetime writes"], rows)
            out("[dim]File-level listing would require mounting the volume; cld does not "
                "do that.[/dim]")

    header("History")
    out(f"Created {created} ({created_ago}) from {src}; Cinder record last changed "
        f"{updated} ({updated_ago}).")
    if history:
        out("cld audit log entries for this volume:")
        for ln in history[-15:]:
            out(f"  [dim]{ln}[/dim]")
    else:
        out("[dim]No cld audit log entries mention this volume (it was never created, "
            "attached or renamed through cld on this controller).[/dim]")


def delete_volume(cloud, volume_id, dry_run=False):
    conn = connect(cloud)
    project_id, project_name = current_project(conn)
    header("Delete volume")

    volume_id = (volume_id or "").strip().lower()
    if not UUID_RE.match(volume_id):
        err("--volumeid must be a full volume UUID (names are not unique; cld will "
            "not guess which volume you mean).")
        return 2
    try:
        volume = conn.block_storage.get_volume(volume_id)
    except os_exc.NotFoundException:
        err(f"volume {volume_id} not found.")
        return 1

    vproj = getattr(volume, "project_id", None)
    if vproj and vproj != project_id:
        err(f"volume belongs to project {vproj}, not {project_name} ({project_id}); "
            "cld only deletes inside the credential's project. Use that project's cloud "
            "entry (cld init --project ...).")
        return 1

    status = getattr(volume, "status", None)
    holders = _nova_holders(conn, volume.id)
    if getattr(volume, "attachments", None) or status == "in-use" or holders:
        _refuse_attached(conn, volume, holders)
        return 1
    if status not in DELETABLE:
        hint = _TRANSITIONAL_HINT.get(status, "find out why (e.g. 'openstack volume "
                                      "show {id}' and the cinder-volume log).")
        err(f"volume is in status '{status}'; cld only deletes volumes that are "
            f"'available' or 'error'. Refusing -- {hint.format(id=volume.id)}")
        return 1

    snaps = [s for s in safe_list(conn.block_storage.snapshots, details=True,
                                  all_projects=True)
             if getattr(s, "volume_id", None) == volume.id]
    backups = [b for b in safe_list(conn.block_storage.backups, details=True,
                                    all_projects=True)
               if getattr(b, "volume_id", None) == volume.id]
    clones = [v for v in safe_list(conn.block_storage.volumes, details=True,
                                   all_projects=True)
              if getattr(v, "source_volid", None) == volume.id]
    out("[dim]Reading the volume's Ceph image (read-only)...[/dim]")
    peek = rbdpeek.peek(volume.id)
    _render_report(volume, snaps, backups, clones, _audit_history(volume.id), peek)

    if peek.get("watchers"):
        err(f"the Ceph image is OPEN by {', '.join(peek['watchers'])} even though Cinder "
            "shows no attachment -- something (a hypervisor, an rbd map) still uses it. "
            "Refusing; find and release that client first.")
        return 1
    if snaps:
        err(f"volume has {len(snaps)} snapshot(s); cld never cascades deletes. Review and "
            "delete the snapshots first if they are really unwanted, then re-run.")
        return 1

    out()
    if backups:
        warn("the backups listed above are separate objects and will REMAIN after the "
             "volume is deleted.")
    if not peek.get("found"):
        warn("its contents were NOT inspected -- you are deciding on metadata alone.")
    if dry_run:
        out("[yellow]--dry-run: nothing deleted.[/yellow]")
        return 0

    name = getattr(volume, "name", "") or "(unnamed)"
    if not confirm_destructive(f"Permanently delete volume {name} ({volume.id}, "
                               f"{volume.size} GB)? This cannot be undone.",
                               default=False):
        out("Kept; nothing deleted.")
        return 0
    try:
        typed = _ask_text(f"Type the first 8 characters of the volume ID "
                          f"({volume.id[:8]}) to confirm: ")
    except Abort:
        typed = None
    if typed != volume.id[:8]:
        out("Confirmation did not match; nothing deleted.")
        return 0

    # Re-read: the decision was made on a snapshot of state; refuse if it moved.
    try:
        fresh = conn.block_storage.get_volume(volume.id)
    except os_exc.NotFoundException:
        warn("the volume disappeared before cld deleted it; nothing to do.")
        return 0
    if (getattr(fresh, "status", None) != status or getattr(fresh, "attachments", None)
            or getattr(fresh, "updated_at", None) != getattr(volume, "updated_at", None)):
        err(f"the volume changed while you were deciding (status now "
            f"'{getattr(fresh, 'status', '?')}'); nothing deleted. Re-run to review it again.")
        return 1

    try:
        out("[bold]Deleting volume...[/bold]")
        # force=False and no cascade: Cinder refuses rather than tearing down
        # attachments/snapshots behind the checks above.
        conn.block_storage.delete_volume(fresh, ignore_missing=False, force=False)
        audit.audit("volume.delete", id=volume.id, name=name, size=volume.size,
                    status=status, created=getattr(volume, "created_at", None),
                    written=peek.get("allocated_bytes"))
    except Exception as e:  # noqa: BLE001
        err(f"delete failed: {e}")
        audit.warn("volume.delete.failed", id=volume.id, error=str(e))
        try:
            now = conn.block_storage.get_volume(volume.id)
            warn(f"volume is now '{getattr(now, 'status', '?')}' (verified).")
        except Exception:  # noqa: BLE001
            warn(f"could not re-read the volume; check 'openstack volume show {volume.id}'.")
        return 1

    # Verify: Cinder deletes asynchronously. Report what we actually observe.
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            now = conn.block_storage.get_volume(volume.id)
        except os_exc.NotFoundException:
            out(f"[green]Deleted:[/green] volume {volume.id} is gone (verified).")
            return 0
        if getattr(now, "status", None) == "error_deleting":
            err(f"Cinder reports 'error_deleting' for {volume.id}; the delete failed on the "
                "backend. Check the cinder-volume log; do not retry blindly.")
            audit.warn("volume.delete.error_deleting", id=volume.id)
            return 1
        time.sleep(3)
    warn(f"volume {volume.id} is still '{getattr(now, 'status', '?')}' after 120 s; "
         f"check with 'openstack volume show {volume.id}'.")
    return 0
