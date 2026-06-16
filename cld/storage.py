"""attachstorage: create a Cinder data volume and attach it to an EXISTING server.

Decoupling storage from VM creation is the structural fix for rc3 issue #112:
this command operates on a pre-existing server, so its rollback can only ever
delete the dangling volume -- it never touches the server.
"""

from cld import audit
from cld.cloud import connect, safe_list
from cld.inventory import server_az
from cld.steps import current_project
from cld.ui import out, header, warn, err, render_table, choose, confirm
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
                   disk=None, dry_run=False):
    conn = connect(cloud)
    project_id, _ = current_project(conn)
    audit.audit("attachstorage.start", cloud=cloud, project=project_id,
                user=getattr(conn, "current_user_id", None), disk=disk)

    server = _resolve_server(conn, project_id, server_arg)
    if server is None:
        return None
    out(f"Target server: [bold]{server.name}[/bold] [dim]({server.id})[/dim]")

    # --disk: attach an EXISTING volume instead of creating a new one.
    if disk:
        return _attach_existing(conn, server, disk, size, type_name, dry_run)

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
        return None

    if not confirm(f"Create and attach a {spec['size']} GB volume to "
                   f"{server.name}?", default=False):
        out("Aborted; nothing created.")
        return None

    return _create_and_attach(conn, server, spec)


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


def _attach_existing(conn, server, volume_id, size, type_name, dry_run):
    """Attach a pre-existing volume to `server`, defensively.

    Refuses unless the volume exists, belongs to the same project, and is
    `available` (unattached/healthy). NEVER creates, modifies, or deletes the
    volume -- on any failure the volume is left exactly as it was.
    """
    if size is not None or type_name is not None:
        warn("--disk given: ignoring --size/--type (attaching the existing "
             "volume as-is).")

    header("Attach existing volume")
    volume = conn.block_storage.find_volume(volume_id, ignore_missing=True)
    if volume is None:
        err(f"volume '{volume_id}' not found in this project")
        return None

    # --- defensive validation (refuse on any failure) ---
    vproj = getattr(volume, "project_id", None)
    sproj = getattr(server, "project_id", None)
    if vproj and sproj and vproj != sproj:
        err(f"volume belongs to a different project ({vproj}) than the server "
            f"({sproj}); cross-project attach is not allowed.")
        return None

    status = getattr(volume, "status", None)
    attachments = getattr(volume, "attachments", None) or []
    if attachments or status == "in-use":
        err(f"volume is already attached to {_attached_servers(conn, volume)}; "
            f"refusing (status={status}).")
        return None
    if status != "available":
        err(f"volume is not available for attach (status={status}); refusing.")
        return None

    # --- non-blocking warnings ---
    if getattr(volume, "is_bootable", False):
        warn("this volume is bootable -- it may be a root/image volume rather "
             "than a spare data disk.")
    vaz = getattr(volume, "availability_zone", None)
    saz = server_az(server)
    if vaz and saz and vaz != saz:
        warn(f"volume AZ '{vaz}' differs from server AZ '{saz}'; Nova may reject "
             "the attach.")

    vname = getattr(volume, "name", "") or "(unnamed)"
    render_table("Existing volume to attach", ["field", "value"],
                 [["volume", f"{vname} ({volume.id})"],
                  ["size", f"{getattr(volume, 'size', '?')} GB"],
                  ["status", status],
                  ["bootable", "yes" if getattr(volume, "is_bootable", False)
                   else "no"],
                  ["AZ", vaz or "-"],
                  ["-> server", f"{server.name} ({server.id})"]])

    if dry_run:
        out()
        out("[yellow]--dry-run: no volume attached.[/yellow]")
        return None

    if getattr(volume, "is_bootable", False):
        if not confirm("This volume is bootable. Attach it anyway?",
                       default=False):
            out("Aborted; nothing attached.")
            return None
    if not confirm(f"Attach existing volume {volume.id} to {server.name}?",
                   default=False):
        out("Aborted; nothing attached.")
        return None

    try:
        out("[bold]Attaching volume...[/bold]")
        conn.compute.create_volume_attachment(server, volume_id=volume.id)
        out(f"[green]Attached volume:[/green] {volume.id} -> {server.name}")
        audit.audit("volume.attach", id=volume.id, server=server.id, existing=True)
    except Exception as e:  # noqa: BLE001
        err(f"attach failed: {e}")
        audit.warn("volume.attach.failed", id=volume.id, server=server.id,
                   existing=True, phase="attach", error=str(e))
        _diagnose_failure("attach", volume, e)
        warn("the volume was not modified or deleted; it remains as it was.")
        return None

    out()
    out(f"[bold green]Done.[/bold green] Volume {volume.id} attached to "
        f"{server.name}")
    return volume


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


def _attach_checks(volume):
    """The three read-only checks that settle transient #111 vs persistent."""
    out("  1. nova-api.log around the failure time -- the wrapped socket error:")
    out("       reset / RemoteDisconnected / BadStatusLine  -> transient #111 (clears on retry)")
    out("       refused / timed out / DNS failure           -> cinder-api unreachable (persistent)")
    out("  2. openstack volume service list  -- are cinder-api/scheduler/volume 'up'?")
    out("  3. re-run attachstorage  -- a keep-alive race clears within a retry or two.")
    vid = getattr(volume, "id", None)
    if vid:
        out(f"[dim]Volume {vid} is unchanged and still 'available'; a re-run retries the attach.[/dim]")


def _diagnose_failure(phase, volume, e):
    """Print an honest, actionable diagnosis after a create/attach failure.

    Classifies only what is visible client-side and points at the read-only checks
    that distinguish a transient #111 keep-alive race from a persistent Cinder
    outage. Adds no retry and triggers no live operation."""
    msg = str(e).lower()

    if phase == "attach":
        # Got an HTTP response from Nova (a 500), vs never reached Nova at all.
        got_500 = any(m in msg for m in _ATTACH_500_MARKERS)
        if any(m in msg for m in _CINDER_CONN_MARKERS):
            warn("Confirmed Nova->Cinder connection failure (rc3 #111 family), not a "
                 "rejection of the request itself.")
            out("[dim]Consistent with the transient HAProxy keep-alive race (#111), but "
                "the 500 alone does not prove it. To be sure:[/dim]")
            _attach_checks(volume)
            return
        if got_500:
            warn("The attach call returned a 500 server error from Nova. On this "
                 "cluster that is most often the #111 Nova->Cinder connection race -- "
                 "but the 500 alone does not prove the cause.")
            _attach_checks(volume)
            return
        if any(m in msg for m in _CLIENT_CONN_MARKERS):
            warn("Could not reach the Nova compute API at all (no HTTP response).")
            out("  - is nova-api up, and the VIP/HAProxy frontend (192.168.1.100:8774) reachable?")
            out("  - openstack server list  -- does any compute call work right now?")
            return
        # Some other attach error (e.g. a 4xx rejection) -- show it plainly.
        warn("Attach was rejected by Nova (not a connection failure). Inspect the "
             "message above and nova-api.log for the specific reason.")
        return

    if phase in ("create", "wait"):
        detail = ("the volume never reached 'available' (stuck creating)"
                  if phase == "wait" else "the volume create call failed")
        warn(f"Failure on the operator->Cinder path: {detail}, before any attach.")
        out("  - openstack volume service list  -- is cinder-volume 'up'?")
        out("  - cld list capacity  -- can this host reach the Cinder API, and is there room?")
        out("  - check the volume type is valid and quota/pool capacity allow the size.")
        return

    warn("Unexpected failure. Inspect nova-api.log and the cinder-api/volume logs "
         "around the failure time for the underlying cause.")


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
        audit.warn("volume.failed", id=getattr(volume, "id", None),
                   server=server.id, phase=phase, error=str(e))
        _diagnose_failure(phase, volume, e)
        _offer_volume_rollback(conn, volume)
        return None

    out()
    out(f"[bold green]Done.[/bold green] Volume {volume.id} attached to "
        f"{server.name}")
    return volume


def _offer_volume_rollback(conn, volume):
    """Offer to delete the dangling volume. Defaults to KEEPING it, and NEVER
    touches the server (it pre-existed this command). Structural #112 guard."""
    if volume is None:
        return
    if not confirm("Delete the volume that failed to attach?", default=False):
        warn("Leaving the volume in place for inspection.")
        return
    try:
        conn.block_storage.delete_volume(volume, ignore_missing=True)
        out("[dim]deleted volume[/dim]")
        audit.audit("volume.delete", id=getattr(volume, "id", None),
                    reason="rollback")
    except Exception as e:  # noqa: BLE001
        warn(f"could not delete volume: {e}")
