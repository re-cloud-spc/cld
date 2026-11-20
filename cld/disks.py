"""createvm: pick existing, available data disks to attach AFTER the VM exists.

Read-only by design. createvm never attaches storage (rc3 #112: a volume
failure must never be able to roll back a healthy VM), so this step only
*selects* volumes and, once the VM is ACTIVE, prints one
`cld attachstorage --serverid <vm> --disk <volume>` line per disk. That path
re-validates everything at attach time and never deletes a pre-existing volume.

Safety rails here:
  - only this project's volumes that are `available` with no attachments;
  - bootable / image volumes (old root disks) are marked and need an explicit
    yes each, default No -- their cloudimg-rootfs/UEFI/BOOT labels can hijack
    the new VM's boot (see storage._warn_boot_label_clash);
  - IDs are shown short (last 4, longer if that is ambiguous) but every printed
    command and warning uses the FULL UUID;
  - every selected disk is re-read before its command is printed; one that is
    no longer available gets no command.
"""

from cld.cloud import safe_list
from cld.ui import out, header, warn, render_table, confirm, choose_indices


def _is_image_volume(v):
    return bool(getattr(v, "is_bootable", False)
                or getattr(v, "volume_image_metadata", None))


def _usable(v, project_id):
    pid = getattr(v, "project_id", None)
    return (getattr(v, "status", None) == "available"
            and not (getattr(v, "attachments", None) or [])
            and (pid is None or project_id is None or pid == project_id))


def short_ids(ids, minimum=4):
    """{full id: shortest unique suffix of at least `minimum` chars}."""
    n = minimum
    while n < 36:
        tails = [i[-n:] for i in ids]
        if len(set(tails)) == len(tails):
            break
        n += 1
    return {i: i[-n:] for i in ids}


def _ts(s):
    return (s or "").replace("T", " ")[:10] or "-"


def available_disks(conn, project_id):
    """(usable volumes sorted by name, count of this project's other volumes)."""
    vols = safe_list(conn.block_storage.volumes, details=True)
    mine = [v for v in vols
            if getattr(v, "project_id", None) in (None, project_id)]
    usable = [v for v in mine if _usable(v, project_id)]
    usable.sort(key=lambda v: ((getattr(v, "name", "") or "").lower(), v.id))
    return usable, len(mine) - len(usable)


def select_disks(conn, project_id, az=None):
    """Optional wizard step. -> list of {id, name, size, bootable} (may be [])."""
    header("Data disks (attached AFTER the VM is created)")
    disks, others = available_disks(conn, project_id)
    if not disks:
        out("[dim]No available (unattached) volumes in this project.[/dim]")
        return []
    sid = short_ids([v.id for v in disks])
    rows = []
    for i, v in enumerate(disks):
        rows.append([i, sid[v.id], getattr(v, "name", "") or "(unnamed)",
                     f"{v.size} GB",
                     "[red]yes ![/red]" if _is_image_volume(v) else "no",
                     getattr(v, "availability_zone", None) or "-",
                     _ts(getattr(v, "created_at", None))])
    render_table("Available volumes in this project (id = last chars)",
                 ["#", "id", "name", "size", "boot", "AZ", "created"], rows)
    out("[dim]boot ! = bootable/image volume (likely an old root disk): needs an "
        "extra yes.[/dim]")
    if others:
        out(f"[dim]{others} other volume(s) here are attached or not "
            "'available' and are not offered.[/dim]")
    out("[dim]Nothing is attached by createvm. Once the VM is ACTIVE you get one "
        "`cld attachstorage --disk` command per disk, which re-checks it.[/dim]")

    picked = []
    for i in choose_indices("Disks to attach after create", len(disks)):
        v = disks[i]
        label = f"{getattr(v, 'name', '') or '(unnamed)'} ({v.id})"
        if _is_image_volume(v):
            warn(f"{label} is bootable / an image volume, probably an old root "
                 "disk. Its 'cloudimg-rootfs' / 'UEFI' / 'BOOT' labels can make "
                 "the NEW VM boot from it after a reboot.")
            if not confirm("Plan to attach it anyway?", default=False):
                out(f"[dim]skipped {label}[/dim]")
                continue
        vaz = getattr(v, "availability_zone", None)
        if az and vaz and vaz != az:
            warn(f"{label} is in AZ '{vaz}', the VM in '{az}'; Nova may refuse "
                 "the attach.")
        picked.append({"id": v.id, "name": getattr(v, "name", "") or "",
                       "size": v.size, "bootable": _is_image_volume(v)})
    if picked:
        out(f"[dim]planned: {', '.join(sid[d['id']] for d in picked)}[/dim]")
    return picked


def recheck_disks(conn, disks, project_id):
    """Re-read each planned disk (read-only). -> the ones still attachable.
    Warns, with the full ID, about every disk that changed since it was picked."""
    ok = []
    for d in disks or []:
        try:
            v = conn.block_storage.get_volume(d["id"])
        except Exception as e:  # noqa: BLE001 - gone or unreadable: no command
            warn(f"disk {d['id']} could not be re-read ({e}); not planning it.")
            continue
        if not _usable(v, project_id):
            pid = getattr(v, "project_id", None)
            if pid and project_id and pid != project_id:
                why = f"it belongs to another project ({pid})"
            else:
                why = (f"it is no longer available (status={getattr(v, 'status', '?')}, "
                       f"attachments={len(getattr(v, 'attachments', None) or [])})")
            warn(f"disk {d['id']}: {why}; not planning it.")
            continue
        ok.append(d)
    return ok


def print_attach_commands(disks, cloud, server_id=None):
    """One attachstorage line per disk; full UUIDs only. No API calls."""
    if not disks:
        return
    cloud_flag = f"--cloud {cloud} " if cloud else ""
    target = server_id or "<new-server-id>"
    out()
    out("[bold]Attach the planned data disks[/bold] [dim](one at a time; each "
        "re-checks the disk and asks before attaching):[/dim]")
    for d in disks:
        tag = "  [red]# bootable: do not reboot until checked[/red]" if d.get("bootable") else ""
        out(f"  cld attachstorage {cloud_flag}--serverid {target} --disk {d['id']}{tag}",
            wrap=False)  # one copy-pasteable line, whatever the width
