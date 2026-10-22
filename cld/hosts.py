"""Per-compute-host capacity from Placement, and the createvm host picker.

Placement is the scheduler's own ledger, so it is the source of truth for "is
there room on this host": at this cluster's Nova microversion the hypervisor
stats (vcpus_used, memory_mb_used) come back as None. Every read here is
read-only and admin-scoped; without the admin role they degrade to an empty
table and the wizard falls back to "let the scheduler decide".

Pinning a host is done with the Nova 2.74 `host` field (see vm.PinnedServer).
Unlike the old `az:host` forced-host syntax, a 2.74 host request still runs the
scheduler filters and the Placement claim, so a host without room fails the
create cleanly instead of being overcommitted.
"""

from cld.ui import out, header, warn, render_table, choose, gb

# 1.17 is enough for inventories (incl. max_unit / allocation_ratio) + usages.
_PLACEMENT = {"OpenStack-API-Version": "placement 1.17"}
_CLASSES = ("VCPU", "MEMORY_MB", "DISK_GB")
_LABEL = {"VCPU": "vCPU", "MEMORY_MB": "RAM", "DISK_GB": "disk"}


def _placement_json(conn, path):
    resp = conn.placement.get(path, headers=_PLACEMENT)
    resp.raise_for_status()
    return resp.json()


def _short(name):
    return (name or "").split(".")[0]


def _host_azs(conn):
    """{service host: AZ} from Nova's AZ listing (admin). The 'internal' AZ
    holds controller services, never compute hosts, so it is skipped."""
    azs = {}
    try:
        for a in conn.compute.availability_zones(details=True):
            if a.name == "internal":
                continue
            for h in (getattr(a, "hosts", None) or {}):
                azs[h] = a.name
    except Exception as e:  # noqa: BLE001
        warn(f"could not read availability zones: {e}")
    return azs


def _compute_services(conn):
    """{service host: 'up' | 'down' | 'disabled'} for nova-compute."""
    states = {}
    try:
        for s in conn.compute.services(binary="nova-compute"):
            if getattr(s, "status", "enabled") == "disabled":
                states[s.host] = "disabled"
            else:
                states[s.host] = getattr(s, "state", None) or "?"
    except Exception as e:  # noqa: BLE001
        warn(f"could not read compute services: {e}")
    return states


def host_capacity(conn, inv=None):
    """One dict per compute host, read from Placement. [] without admin.

    Each dict: host, az, state, servers, and per resource class
    {VCPU,MEMORY_MB,DISK_GB}: {"used", "cap", "free", "max_unit"} where
    cap = (total - reserved) * allocation_ratio -- exactly what the Placement
    claim checks. SHUTOFF servers still hold their allocation, so they count.
    """
    try:
        providers = _placement_json(conn, "/resource_providers")["resource_providers"]
    except Exception as e:  # noqa: BLE001 - no admin / no placement endpoint
        warn(f"could not read Placement resource providers: {e}")
        return []

    azs = _host_azs(conn)
    states = _compute_services(conn)
    counts = {}
    for s in (inv.servers if inv is not None else []):
        h = getattr(s, "compute_host", None)
        if h:
            counts[h] = counts.get(h, 0) + 1

    rows = []
    for rp in providers:
        if rp.get("parent_provider_uuid"):
            continue  # nested providers (vGPU, ...) are not compute hosts
        uuid, name = rp["uuid"], rp["name"]
        # Nova's `host` field wants the service host; the provider is named after
        # the hypervisor hostname, which may be its FQDN.
        host = name if name in states else _short(name)
        try:
            invs = _placement_json(conn, f"/resource_providers/{uuid}/inventories")["inventories"]
            usage = _placement_json(conn, f"/resource_providers/{uuid}/usages")["usages"]
        except Exception as e:  # noqa: BLE001
            warn(f"could not read Placement for {name}: {e}")
            continue
        if "MEMORY_MB" not in invs:
            continue  # not a compute node (e.g. a shared storage provider)
        row = {"host": host, "az": azs.get(host, "?"),
               "state": states.get(host, "?"), "servers": counts.get(host, 0)}
        for rc in _CLASSES:
            i = invs.get(rc)
            if not i:
                continue
            cap = int((i["total"] - i.get("reserved", 0)) * i.get("allocation_ratio", 1.0))
            used = usage.get(rc, 0)
            row[rc] = {"used": used, "cap": cap, "free": cap - used,
                       "max_unit": i.get("max_unit", cap)}
        rows.append(row)
    rows.sort(key=lambda r: r["host"])
    return rows


def _needs(flavor):
    """The Placement request a flavor makes. Root disk counts only for
    image-backed boots, which is what createvm does."""
    return {"VCPU": getattr(flavor, "vcpus", 0) or 0,
            "MEMORY_MB": getattr(flavor, "ram", 0) or 0,
            "DISK_GB": getattr(flavor, "disk", 0) or 0}


def host_fit(row, flavor):
    """-> None if `flavor` fits on this host right now, else a short reason.

    Mirrors the scheduler's hard checks: service up+enabled, and for each
    resource class both the free capacity and the per-allocation max_unit.
    """
    if row["state"] != "up":
        return f"service {row['state']}"
    for rc, need in _needs(flavor).items():
        r = row.get(rc)
        if not need or r is None:
            continue
        if need > r["max_unit"]:
            return f"{_LABEL[rc]} {need} exceeds the per-VM max {r['max_unit']}"
        if need > r["free"]:
            if rc == "MEMORY_MB":
                return f"needs {need / 1024:.1f} GiB RAM, {r['free'] / 1024:.1f} GiB free"
            return f"needs {need} {_LABEL[rc]}, {r['free']} free"
    return None


def _pct(r):
    return f"{100 * r['used'] / r['cap']:.0f}%" if r and r["cap"] else "?"


def render_hosts(conn, inv, flavor=None, rows=None):
    """Per-host capacity table (read-only). With `flavor`, a narrower table of
    what is free, what would be left after placing it, and whether it fits."""
    rows = host_capacity(conn, inv) if rows is None else rows
    if not rows:
        out("[dim]Per-host capacity unavailable (Placement needs the admin role).[/dim]")
        return rows
    header("Compute hosts (Placement)")
    if flavor is None:
        columns = ["host", "AZ", "state", "servers", "vCPU used/cap",
                   "RAM GiB used/cap", "RAM free"]
    else:
        columns = ["host", "AZ", "state", "vCPU free", "RAM free",
                   f"RAM after {flavor.name}", "fits"]
    table, reasons = [], []
    for r in rows:
        v, m = r.get("VCPU"), r.get("MEMORY_MB")
        free = f"{m['free'] / 1024:.1f} GiB"
        if flavor is None:
            table.append([r["host"], r["az"], r["state"], r["servers"],
                          f"{v['used']}/{v['cap']}" if v else "?",
                          f"{gb(m['used'] / 1024)}/{gb(m['cap'] / 1024)} ({_pct(m)})",
                          free])
            continue
        reason = host_fit(r, flavor)
        after = (m["free"] - _needs(flavor)["MEMORY_MB"]) / 1024
        table.append([r["host"], r["az"], r["state"], v["free"] if v else "?", free,
                      "-" if reason else f"{after:.1f} GiB",
                      "[red]no[/red]" if reason else "[green]yes[/green]"])
        if reason:
            reasons.append(f"{r['host']}: {reason}")
    render_table("Per-host allocation (what the scheduler claims against)",
                 columns, table)
    for line in reasons:
        out(f"[dim]  no room on {line}[/dim]")
    out("[dim]Capacity = (total - reserved) x allocation ratio, the cap the "
        "Placement claim enforces; SHUTOFF servers still hold their allocation.[/dim]")
    return rows


def select_host(conn, inv, flavor, az=None):
    """Optionally pin the VM to one compute host. -> host name or None.

    None (Enter) = let the scheduler decide, the default. Only hosts that are in
    the chosen AZ and fit the flavor right now are offered; the menu lists them
    by most free RAM first, which is what the default RAM weigher prefers too.
    """
    rows = host_capacity(conn, inv)
    if az:
        rows = [r for r in rows if r["az"] == az]
    if not rows:
        out("[dim]No per-host data; the scheduler will place the VM.[/dim]")
        return None
    render_hosts(conn, inv, flavor=flavor, rows=rows)
    fits = [r for r in rows if host_fit(r, flavor) is None]
    if not fits:
        warn(f"no host{' in AZ ' + az if az else ''} has room for flavor "
             f"'{flavor.name}' right now -- the create would fail with "
             "'No valid host'. Pick a smaller flavor or free capacity first.")
        return None
    fits.sort(key=lambda r: -r["MEMORY_MB"]["free"])
    out("[dim]Pinning uses Nova's `host` field (admin only): the scheduler still "
        "checks the host, so a full host is refused, never overcommitted.[/dim]")
    pick = choose("Pin to compute host", fits,
                  label=lambda r: f"{r['host']}  [dim]({r['az']}, "
                                  f"{r['MEMORY_MB']['free'] / 1024:.1f} GiB RAM, "
                                  f"{r.get('VCPU', {}).get('free', '?')} vCPU free)[/dim]",
                  allow_none=True, none_label="(let scheduler decide)")
    return pick["host"] if pick else None
