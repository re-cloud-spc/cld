"""createvm: build the server payload, reserve a static IP, create the server,
optionally assign a floating IP. Data volumes are intentionally NOT handled here
-- attach them separately with `cld attachstorage` (this keeps a transient Cinder
failure from ever rolling back a healthy, ACTIVE server; see rc3 issue #112).
"""

import base64

from openstack import exceptions as os_exc

from cld import audit
from cld.steps import next_available_ip
from cld.ui import out, header, warn, err, render_table, confirm


def key_names(security):
    """The selected SSH keypair names as a list, tolerating a legacy single
    `key_name` (str) from specs saved before multi-key support."""
    names = security.get("key_names")
    if names is None:
        legacy = security.get("key_name")
        names = [legacy] if legacy else []
    return [n for n in names if n]


def _keypair_user_data(conn, names):
    """Base64 #cloud-config that authorizes the given keypairs' public keys.

    Nova injects only one keypair at boot (`key_name`), so the *extra* keys are
    delivered via cloud-init. Returns None if there are no keys or none have a
    readable public key.
    """
    keys = []
    for n in names:
        try:
            kp = conn.compute.get_keypair(n)
            pub = getattr(kp, "public_key", None)
            if pub:
                keys.append(pub.strip())
        except Exception as e:  # noqa: BLE001
            warn(f"could not read public key for keypair '{n}': {e}")
    if not keys:
        return None
    body = "\n".join(["#cloud-config", "ssh_authorized_keys:"]
                     + [f"  - {k}" for k in keys]) + "\n"
    return base64.b64encode(body.encode()).decode()


def build_payload(spec, conn=None, port_id=None):
    net = spec["network"]
    if port_id:
        networks = [{"port": port_id}]
    elif net.get("fixed_ip"):
        # dry-run display only; real creates reserve a dedicated port instead
        networks = [{"uuid": net["network_id"], "fixed_ip": net["fixed_ip"]}]
    else:
        networks = [{"uuid": net["network_id"]}]
    payload = {
        "name": spec["name"],
        "image_id": spec["image_id"],
        "flavor_id": spec["flavor_id"],
        "networks": networks,
    }
    if spec.get("az"):
        payload["availability_zone"] = spec["az"]
    names = key_names(spec["security"])
    if names:
        # First key -> Nova keypair (keeps the Horizon/CLI association); any
        # remaining keys -> cloud-init user_data. Needs conn to read public keys.
        payload["key_name"] = names[0]
        if len(names) > 1 and conn is not None:
            user_data = _keypair_user_data(conn, names[1:])
            if user_data:
                payload["user_data"] = user_data
    if spec["security"].get("security_groups"):
        payload["security_groups"] = [
            {"name": n} for n in spec["security"]["security_groups"]]
    return payload


def _reserve_port(conn, spec):
    """Reserve the next free internal IP as a dedicated port, retrying on race.

    Returns the port, or None if no IP could be reserved (caller falls back to
    Neutron auto-assignment).
    """
    net = spec["network"]
    ip = net.get("fixed_ip")
    if not ip:
        return None
    subnet = conn.network.get_subnet(net["subnet_id"])
    tried = set()
    for _ in range(5):
        if not ip:
            break
        try:
            port = conn.network.create_port(
                network_id=net["network_id"], name=f"{spec['name']}-port",
                fixed_ips=[{"subnet_id": net["subnet_id"], "ip_address": ip}])
            out(f"[green]Reserved port:[/green] {port.id} (fixed IP {ip})")
            audit.audit("port.reserve", id=port.id, ip=ip, name=f"{spec['name']}-port")
            return port
        except os_exc.SDKException as e:  # IP already allocated / race
            warn(f"IP {ip} unavailable ({e}); recomputing next free address")
            tried.add(ip)
            ip = next_available_ip(conn, subnet, exclude=tried)
    warn("could not reserve a static IP; Neutron will auto-assign.")
    return None


def create_vm(conn, spec):
    port = _reserve_port(conn, spec)
    payload = build_payload(spec, conn=conn, port_id=port.id if port else None)
    out()
    out("[bold]Creating server...[/bold]")
    try:
        server = conn.compute.create_server(**payload)
        audit.audit("server.create", id=server.id, name=spec["name"],
                    keys=len(key_names(spec["security"])))
    except Exception as e:  # noqa: BLE001
        err(f"server create failed: {e}")
        audit.warn("server.create.failed", name=spec["name"], error=str(e))
        _offer_rollback(conn, None, port)
        return None
    try:
        server = conn.compute.wait_for_server(server, wait=600)
        out(f"[green]Server ACTIVE:[/green] {server.name} ({server.id})")
        audit.audit("server.active", id=server.id, name=server.name)
    except Exception as e:  # noqa: BLE001
        err(f"server did not become active: {e}")
        audit.warn("server.active.failed", id=server.id, error=str(e))
        _offer_rollback(conn, server, port)
        return None

    if spec["security"].get("floating_ip"):
        try:
            out("[bold]Assigning floating IP...[/bold]")
            ip = conn.add_auto_ip(server, wait=True)
            out(f"[green]Floating IP:[/green] {ip}")
            audit.audit("floatingip.assign", server=server.id, ip=ip)
        except Exception as e:  # noqa: BLE001
            # The server is healthy; a floating-IP failure never rolls it back.
            err(f"floating IP assignment failed (server still running): {e}")
            audit.warn("floatingip.failed", server=server.id, error=str(e))

    out()
    out(f"[bold green]Done.[/bold green] Server {server.id}")
    cloud = spec.get("cloud")
    cloud_flag = f"--cloud {cloud} " if cloud else ""
    out(f"[dim]To add a data volume, run:[/dim]\n"
        f"  cld attachstorage {cloud_flag}--serverid {server.id}")
    return server


def _offer_rollback(conn, server, port=None):
    """Offer to delete partially-created resources. Defaults to KEEPING them.

    Reached only when server create/wait FAILED, so any `server` here is not a
    healthy ACTIVE VM. Pressing Enter keeps everything for inspection
    (default=False) -- the tool never deletes on an unattended keystroke
    (rc3 issue #112).
    """
    if not confirm("Roll back (delete the partially-created resources)?",
                   default=False):
        warn("Leaving resources in place for inspection.")
        return
    # Delete the server first so it releases the port, then the port.
    if server is not None:
        try:
            conn.compute.delete_server(server, ignore_missing=True)
            out("[dim]deleted server[/dim]")
            audit.audit("server.delete", id=getattr(server, "id", None),
                        reason="rollback")
        except Exception as e:  # noqa: BLE001
            warn(f"could not delete server: {e}")
    if port is not None:
        try:
            conn.network.delete_port(port, ignore_missing=True)
            out("[dim]deleted reserved port[/dim]")
            audit.audit("port.delete", id=getattr(port, "id", None),
                        reason="rollback")
        except Exception as e:  # noqa: BLE001
            warn(f"could not delete port: {e}")


def print_summary(spec):
    header("Summary")
    rows = [
        ["name", spec["name"]],
        ["project", spec.get("project_name", "(current scope)")],
        ["availability zone", spec.get("az") or "(scheduler)"],
        ["flavor", spec.get("flavor_name")],
        ["image", spec.get("image_name")],
        ["network/subnet", f"{spec['network']['network_name']} "
                           f"({'external' if spec['network']['external'] else 'tenant'})"],
        ["internal IP", spec["network"].get("fixed_ip") or "(auto-assigned)"],
        ["keypairs", ", ".join(key_names(spec["security"])) or "(none!)"],
        ["security groups", ", ".join(spec["security"].get("security_groups") or [])
                            or "(default)"],
        ["floating IP", "yes" if spec["security"].get("floating_ip") else "no"],
    ]
    render_table("VM to create", ["field", "value"], rows)
