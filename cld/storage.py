"""attachstorage: create a Cinder data volume and attach it to an EXISTING server.

Decoupling storage from VM creation is the structural fix for rc3 issue #112:
this command operates on a pre-existing server, so its rollback can only ever
delete the dangling volume -- it never touches the server.
"""

from cld import audit
from cld.cloud import connect, safe_list
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
                   dry_run=False):
    conn = connect(cloud)
    project_id, _ = current_project(conn)
    audit.audit("attachstorage.start", cloud=cloud, project=project_id,
                user=getattr(conn, "current_user_id", None))

    server = _resolve_server(conn, project_id, server_arg)
    if server is None:
        return None
    out(f"Target server: [bold]{server.name}[/bold] [dim]({server.id})[/dim]")

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


def _create_and_attach(conn, server, spec):
    volume = None
    try:
        out("[bold]Creating data volume...[/bold]")
        vargs = {"name": f"{server.name}-data", "size": spec["size"]}
        if spec.get("type"):
            vargs["volume_type"] = spec["type"]
        volume = conn.block_storage.create_volume(**vargs)
        audit.audit("volume.create", id=volume.id, size=spec["size"],
                    type=spec.get("type"))
        volume = conn.block_storage.wait_for_status(volume, status="available",
                                                    wait=300)
        conn.compute.create_volume_attachment(server, volume_id=volume.id)
        out(f"[green]Attached volume:[/green] {volume.id} ({spec['size']} GB) "
            f"-> {server.name}")
        audit.audit("volume.attach", id=volume.id, server=server.id)
    except Exception as e:  # noqa: BLE001
        err(f"volume create/attach failed: {e}")
        audit.warn("volume.failed", id=getattr(volume, "id", None),
                   server=server.id, error=str(e))
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
