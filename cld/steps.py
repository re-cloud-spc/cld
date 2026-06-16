"""Interactive wizard steps for `createvm`: project, AZ, flavor, image, network,
security. Each step prints the relevant live inventory before prompting.

As the tool grows this module is the natural seam to split into a steps/
subpackage (one module per step).
"""

import ipaddress

from cld.cloud import safe_list
from cld.ui import out, header, warn, err, render_table, choose, choose_multi, gb


# --------------------------------------------------------------------------- #
# Project
# --------------------------------------------------------------------------- #
def current_project(conn):
    """Return (project_id, project_name) for the app-cred-scoped connection.

    Application credentials are bound to a single project (cannot be re-scoped),
    so the project is fixed by the chosen cloud entry rather than selected here.
    """
    pid = getattr(conn, "current_project_id", None)
    name = None
    if pid:
        try:
            proj = conn.identity.get_project(pid)
            name = getattr(proj, "name", None)
        except Exception:  # noqa: BLE001 - may lack identity read on this scope
            name = None
    header("Project")
    out(f"Scoped to project: [bold]{name or pid or '(unknown)'}[/bold]"
        f"  [dim]({pid or 'n/a'})[/dim]")
    out("[dim]Project is fixed by the app credential. To target a different "
        "project, pick another cloud or add one with `cld init`.[/dim]")
    return pid, name


# --------------------------------------------------------------------------- #
# Availability zone
# --------------------------------------------------------------------------- #
def az_capacity_rows(conn, inv):
    """Per-AZ compute capacity via aggregates + hypervisors (admin)."""
    aggregates = safe_list(conn.compute.aggregates)
    hypervisors = safe_list(conn.compute.hypervisors, details=True)

    host_to_az = {}
    for agg in aggregates:
        az = getattr(agg, "availability_zone", None)
        if az:
            for host in (getattr(agg, "hosts", None) or []):
                host_to_az[host] = az

    az_stats = {}
    for h in hypervisors:
        name = getattr(h, "name", None) or getattr(h, "hypervisor_hostname", "")
        host = name.split(".")[0]
        az = host_to_az.get(host) or host_to_az.get(name) or "(unmapped)"
        s = az_stats.setdefault(az, {"vcpu": 0, "vcpu_used": 0, "ram": 0,
                                     "ram_used": 0, "hosts": 0})
        s["hosts"] += 1
        s["vcpu"] += getattr(h, "vcpus", 0) or 0
        s["vcpu_used"] += getattr(h, "vcpus_used", 0) or 0
        s["ram"] += getattr(h, "memory_size", 0) or getattr(h, "memory_mb", 0) or 0
        s["ram_used"] += (getattr(h, "memory_used", 0)
                          or getattr(h, "memory_mb_used", 0) or 0)

    srv_counts = inv.count_by_az()
    rows = []
    for az, s in sorted(az_stats.items()):
        rows.append([
            az, s["hosts"],
            f"{s['vcpu_used']}/{s['vcpu']}",
            f"{gb(s['ram_used'])}/{gb(s['ram'])} MB",
            srv_counts.get(az, 0),
        ])
    return rows


def select_az(conn, inv):
    nova_azs = safe_list(conn.compute.availability_zones, details=True)
    names = [getattr(a, "name", None) for a in nova_azs
             if getattr(a, "name", None)]
    names = [n for n in names if n]

    if len(names) <= 1:
        if names:
            out(f"Single availability zone: [bold]{names[0]}[/bold] "
                "(auto-selected)")
            return names[0]
        out("[dim]No nova availability zones reported; leaving AZ unset.[/dim]")
        return None

    render_azs(conn, inv)
    return choose("Select availability zone", names, allow_none=True,
                  none_label="(let scheduler decide)")


def render_azs(conn, inv):
    """Print per-AZ compute capacity + the Cinder/Neutron AZ lists (read-only)."""
    header("Availability zones")
    cap = az_capacity_rows(conn, inv)
    if cap:
        render_table("Compute capacity per AZ",
                     ["AZ", "hosts", "vCPU used/total", "RAM used/total",
                      "servers"], cap)

    cinder_azs = [getattr(a, "name", "?")
                  for a in safe_list(conn.block_storage.availability_zones)]
    net_azs = [getattr(a, "name", "?")
               for a in safe_list(conn.network.availability_zones)]
    out(f"[dim]Cinder AZs:[/dim] {', '.join(cinder_azs) or '(n/a)'}")
    out(f"[dim]Neutron AZs:[/dim] {', '.join(net_azs) or '(n/a)'}")


# --------------------------------------------------------------------------- #
# Flavor
# --------------------------------------------------------------------------- #
def render_flavors(conn, inv):
    """Print the flavor table (read-only); return the sorted flavor list."""
    header("Flavor")
    flavors = safe_list(conn.compute.flavors, details=True)
    flavors.sort(key=lambda f: (getattr(f, "vcpus", 0), getattr(f, "ram", 0)))
    counts = inv.count_by_flavor()
    rows = []
    for f in flavors:
        used = counts.get(f.name, 0) + counts.get(f.id, 0)
        rows.append([f.name, f.vcpus, f"{gb(f.ram)} MB",
                     f"{f.disk} GB", getattr(f, "ephemeral", 0) or 0,
                     "yes" if getattr(f, "is_public", True) else "no", used])
    render_table("Flavors (root 'disk' is the small Ceph-backed boot disk)",
                 ["name", "vCPU", "RAM", "root disk", "ephem", "public",
                  "in use"], rows)
    return flavors


def select_flavor(conn, inv):
    flavors = render_flavors(conn, inv)
    return choose("Select flavor", flavors,
                  label=lambda f: f"{f.name}  ({f.vcpus} vCPU / {gb(f.ram)}MB / "
                                  f"{f.disk}GB)")


# --------------------------------------------------------------------------- #
# Image
# --------------------------------------------------------------------------- #
def render_images(conn, inv):
    """Print the image table (read-only); return the sorted image list."""
    header("Image")
    images = safe_list(conn.image.images)
    images.sort(key=lambda i: (getattr(i, "name", "") or "").lower())
    counts = inv.count_by_image()
    rows = []
    for i in images:
        size_gb = (i.size or 0) / (1024 ** 3) if getattr(i, "size", None) else 0
        rows.append([
            i.name, getattr(i, "visibility", "?"),
            f"{size_gb:,.1f} GB",
            f"{getattr(i, 'min_disk', 0) or 0} GB",
            f"{getattr(i, 'min_ram', 0) or 0} MB",
            "yes" if i.to_dict().get("img_signature") else "no",
            counts.get(i.id, 0),
        ])
    render_table("Images (boot-from-volume servers don't count in 'in use')",
                 ["name", "visibility", "size", "min disk", "min ram",
                  "signed", "in use"], rows)
    return images


def select_image(conn, inv):
    images = render_images(conn, inv)
    img = choose("Select image", images,
                 label=lambda i: f"{i.name}  [{getattr(i, 'visibility', '?')}]")
    if img and getattr(img, "visibility", "") == "community":
        warn("This is a community image (unvetted owner). Verify you trust it.")
    return img


# --------------------------------------------------------------------------- #
# Network / subnet
# --------------------------------------------------------------------------- #
def next_available_ip(conn, subnet, exclude=None):
    """Lowest free IPv4 in the subnet's allocation pools.

    Excludes addresses already used by ports on the network, the gateway, and
    anything in `exclude`. Returns a string IP, or None for IPv6 / no free
    address (caller then falls back to Neutron auto-assignment).
    """
    exclude = set(exclude or ())
    if getattr(subnet, "ip_version", 4) != 4:
        return None

    used = set(str(x) for x in exclude)
    gw = getattr(subnet, "gateway_ip", None)
    if gw:
        used.add(str(gw))
    for p in safe_list(conn.network.ports, network_id=subnet.network_id):
        for fip in (getattr(p, "fixed_ips", None) or []):
            if fip.get("subnet_id") == subnet.id and fip.get("ip_address"):
                used.add(fip["ip_address"])

    pools = getattr(subnet, "allocation_pools", None) or []
    if pools:
        ranges = []
        for pool in pools:
            try:
                ranges.append((int(ipaddress.ip_address(pool["start"])),
                               int(ipaddress.ip_address(pool["end"]))))
            except (KeyError, ValueError):
                continue
        candidates = (ipaddress.ip_address(i)
                      for lo, hi in ranges for i in range(lo, hi + 1))
    else:
        try:
            candidates = ipaddress.ip_network(subnet.cidr, strict=False).hosts()
        except ValueError:
            return None

    for ip in candidates:
        if str(ip) not in used:
            return str(ip)
    return None


def render_networks(conn, project_id):
    """Print the networks/subnets table (read-only); return the list of
    selectable (network, subnet, is_external) tuples."""
    header("Network / subnet")
    networks = safe_list(conn.network.networks)
    if project_id:
        networks = [n for n in networks
                    if n.project_id in (project_id, None)
                    or getattr(n, "is_shared", False)
                    or getattr(n, "is_router_external", False)]
    ports = safe_list(conn.network.ports)
    used_ips_by_subnet = {}
    for p in ports:
        for fip in (getattr(p, "fixed_ips", None) or []):
            sid = fip.get("subnet_id")
            if sid:
                used_ips_by_subnet[sid] = used_ips_by_subnet.get(sid, 0) + 1

    rows = []
    selectable = []
    for n in networks:
        ext = getattr(n, "is_router_external", False)
        shared = getattr(n, "is_shared", False)
        subnets = safe_list(conn.network.subnets, network_id=n.id)
        if not subnets:
            rows.append([n.name, "(no subnets)", "", "",
                         "external" if ext else ("shared" if shared else "")])
            continue
        for sub in subnets:
            used = used_ips_by_subnet.get(sub.id, 0)
            rows.append([
                n.name, sub.cidr, getattr(sub, "gateway_ip", "") or "",
                f"{used} ports",
                "external" if ext else ("shared" if shared else "tenant"),
            ])
            selectable.append((n, sub, ext))

    render_table("Networks / subnets",
                 ["network", "cidr", "gateway", "in use", "kind"], rows)
    return selectable


def select_network(conn, project_id):
    from cld.ui import confirm
    selectable = render_networks(conn, project_id)
    if not selectable:
        err("no usable subnets found for this project")
        return None

    chosen = choose("Select network/subnet", selectable,
                    label=lambda t: f"{t[0].name} / {t[1].cidr}"
                    + ("  [EXTERNAL]" if t[2] else ""))
    net, sub, ext = chosen
    if ext:
        warn("This is an EXTERNAL network - the VM would be directly exposed.")
        if not confirm("Really attach directly to an external network?",
                       default=False):
            return select_network(conn, project_id)

    fixed_ip = next_available_ip(conn, sub)
    if fixed_ip:
        out(f"Next available internal IP: [bold]{fixed_ip}[/bold] "
            f"[dim](reserved via a dedicated port at create time)[/dim]")
    else:
        warn("could not compute a free IPv4 (IPv6 subnet or pool exhausted); "
             "Neutron will auto-assign.")
    return {"network_id": net.id, "network_name": net.name,
            "subnet_id": sub.id, "external": ext, "fixed_ip": fixed_ip}


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #
def _rule_is_world_open(rule):
    if rule.get("direction") != "ingress":
        return False
    remote = rule.get("remote_ip_prefix")
    return remote in ("0.0.0.0/0", "::/0")


def security_review(conn):
    from cld.ui import confirm
    header("Security")

    # Keypair(s) - multiple may be selected; the VM authorizes all of them.
    keypairs = safe_list(conn.compute.keypairs)
    key_names = []
    if keypairs:
        chosen = choose_multi("SSH keypairs to inject", keypairs,
                              label=lambda k: k.name,
                              none_label="(no keypair - NOT recommended)")
        key_names = [k.name for k in chosen]
    else:
        warn("no keypairs found; create one with: openstack keypair create ...")
    if not key_names:
        warn("No SSH key will be injected. You may be locked out unless the "
             "image has another access method.")
    elif len(key_names) > 1:
        out(f"[dim]selected: {', '.join(key_names)}[/dim]")
        warn("Multiple keys: the first is the Nova keypair; the rest are injected "
             "via cloud-init, so the image must support cloud-init.")

    # Security groups
    sgs = safe_list(conn.network.security_groups)
    rows = []
    for sg in sgs:
        open_rules = [r for r in (sg.security_group_rules or [])
                      if _rule_is_world_open(r)]
        flag = ""
        if open_rules:
            ports = sorted({str(r.get("port_range_min") or "all")
                            for r in open_rules})
            flag = f"world-open ingress: {', '.join(ports)}"
        rows.append([sg.name, len(sg.security_group_rules or []), flag])
    render_table("Security groups", ["name", "#rules", "warning"], rows)

    chosen_sgs = []
    if sgs:
        while True:
            sg = choose("Add a security group (repeat; skip to finish)", sgs,
                        label=lambda s: s.name, allow_none=True,
                        none_label="(done adding)")
            if sg is None:
                break
            if any(_rule_is_world_open(r) for r in (sg.security_group_rules or [])):
                warn(f"'{sg.name}' allows ingress from anywhere (0.0.0.0/0).")
                if not confirm("Add it anyway?", default=False):
                    continue
            if sg.name not in chosen_sgs:
                chosen_sgs.append(sg.name)
            out(f"[dim]selected: {', '.join(chosen_sgs)}[/dim]")
    if not chosen_sgs:
        warn("No security group selected; the project 'default' SG will apply "
             "(allows all intra-group traffic, blocks external ingress).")

    # Floating IP
    floating = confirm("Assign a floating (public) IP after boot?",
                       default=False)
    if floating:
        warn("A floating IP exposes this VM to the network the pool routes to.")
        if not confirm("Confirm public exposure via floating IP?", default=False):
            floating = False

    return {"key_names": key_names, "security_groups": chosen_sgs,
            "floating_ip": floating}
