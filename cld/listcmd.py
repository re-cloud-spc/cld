"""list: read-only inventory of the OpenStack cloud.

Reuses the same table renderers as the createvm wizard (cld.steps.render_*) plus
the Cinder capacity view (cld.volume.show_capacity). Purely read-only -- it makes
no writes and intentionally records nothing to the audit log.
"""

from cld.cloud import connect, select_cloud, safe_list, list_cloud_names
from cld.inventory import Inventory, server_az
from cld.steps import render_flavors, render_images, render_networks, render_azs
from cld.ui import out, header, render_table
from cld.volume import show_capacity

RESOURCES = ["clouds", "servers", "flavors", "images", "networks", "azs",
             "capacity"]


def run_list(resource, cloud_arg, all_projects=False):
    # 'clouds' is a local read of clouds.yaml -- no connection / auth needed.
    if resource == "clouds":
        return _render_clouds()

    cloud = select_cloud(cloud_arg)
    conn = connect(cloud)
    project_id = getattr(conn, "current_project_id", None)
    out(f"Cloud [bold]{cloud}[/bold]  [dim](project {project_id or '?'})[/dim]")

    if resource == "servers":
        _render_servers(conn, all_projects)
    elif resource == "flavors":
        render_flavors(conn, Inventory(conn))
    elif resource == "images":
        render_images(conn, Inventory(conn))
    elif resource == "networks":
        render_networks(conn, project_id)
    elif resource == "azs":
        render_azs(conn, Inventory(conn))
    elif resource == "capacity":
        header("Capacity")
        show_capacity(conn, project_id)
    return 0


def _render_clouds():
    header("Clouds (clouds.yaml entries)")
    names = list_cloud_names()
    rows = [[n, "application credential (one project)"] for n in names]
    render_table("Configured clouds", ["cloud (= project)", "auth"], rows)
    out("[dim]Each entry is bound to one project. Add more with `cld init`.[/dim]")
    return 0


def _server_ips(server):
    """Flatten the server.addresses dict into a comma-joined string of IPs."""
    ips = []
    for entries in (getattr(server, "addresses", None) or {}).values():
        for e in entries or []:
            addr = e.get("addr") if isinstance(e, dict) else None
            if addr:
                ips.append(addr)
    return ", ".join(ips) or "-"


def _render_servers(conn, all_projects):
    header("Servers" + (" (all projects)" if all_projects else ""))
    servers = safe_list(conn.compute.servers, details=True,
                        all_projects=all_projects)
    servers.sort(key=lambda s: (getattr(s, "name", "") or "").lower())
    columns = ["name", "id", "status", "flavor", "IP", "AZ"]
    if all_projects:
        columns.append("project")
    rows = []
    for s in servers:
        flavor = (s.flavor or {}).get("original_name") or "?"
        row = [s.name, s.id, getattr(s, "status", "?"), flavor,
               _server_ips(s), server_az(s) or "-"]
        if all_projects:
            row.append(getattr(s, "project_id", None) or "-")
        rows.append(row)
    render_table("Servers", columns, rows)
