"""Data-volume capacity reporting + spec prompting (Ceph/RBD-backed Cinder).

Used by `attachstorage`. (createvm no longer creates volumes — storage is a
separate, isolated step.) The old single configure_volume() is split into:
  show_capacity(conn, project_id) -> (pool_free, quota_gb)   # prints SDS + quota
  prompt_volume_spec(conn, pool_free, quota_gb) -> {size, type}
"""

from cld.cloud import safe_list
from cld.ui import out, warn, render_table, choose, prompt_int, gb


def show_sds_capacity(conn):
    pools = safe_list(conn.block_storage.backend_pools)
    rows = []
    for p in pools:
        caps = getattr(p, "capabilities", None) or {}
        rows.append([
            getattr(p, "name", caps.get("volume_backend_name", "?")),
            f"{gb(caps.get('total_capacity_gb'))} GB",
            f"{gb(caps.get('free_capacity_gb'))} GB",
            f"{gb(caps.get('allocated_capacity_gb'))} GB",
            caps.get("max_over_subscription_ratio", "?"),
        ])
    render_table("SDS / Cinder pool capacity (Ceph)",
                 ["pool", "total", "free", "allocated", "oversub"], rows)
    # Best-effort total free across pools for validation.
    free = 0.0
    for p in pools:
        caps = getattr(p, "capabilities", None) or {}
        try:
            free += float(caps.get("free_capacity_gb") or 0)
        except (TypeError, ValueError):
            pass
    return free or None


def show_volume_quota(conn, project_id):
    if not project_id:
        return None
    try:
        q = conn.block_storage.get_quota_set(project_id, usage=True)
        d = q.to_dict()
        usage = d.get("usage") or {}
        gigs = d.get("gigabytes")
        used = (usage.get("gigabytes") or {})
        used_gb = used.get("in_use") if isinstance(used, dict) else None
        render_table("Project volume quota",
                     ["metric", "limit", "in use"],
                     [["gigabytes", gigs, used_gb if used_gb is not None else "?"],
                      ["volumes", d.get("volumes"), "-"]])
        return gigs
    except Exception as e:  # noqa: BLE001
        warn(f"could not read volume quota: {e}")
        return None


def _is_encrypted_type(conn, vtype):
    try:
        enc = conn.block_storage.get_type_encryption(vtype.id)
        return bool(getattr(enc, "encryption_id", None)
                    or getattr(enc, "provider", None))
    except Exception:  # noqa: BLE001
        return False


def show_capacity(conn, project_id):
    """Print SDS pool capacity + project quota; return (pool_free, quota_gb)."""
    pool_free = show_sds_capacity(conn)
    quota_gb = show_volume_quota(conn, project_id)
    return pool_free, quota_gb


def prompt_volume_spec(conn, pool_free, quota_gb, size=None, type_name=None):
    """Prompt (or accept supplied) volume size + type. Returns {size, type}.

    Validates the requested size against pool free space and project quota,
    warning (not blocking) on overrun. With size/type pre-supplied (non-
    interactive), it validates and skips the prompts.
    """
    max_gb = int(pool_free) if pool_free is not None else None
    if size is None:
        size = prompt_int("Volume size in GB", minimum=1, maximum=max_gb)
    if size < 1:
        warn("volume size must be >= 1 GB")
        return None

    if pool_free is not None and size > pool_free:
        warn(f"requested {size} GB exceeds pool free {gb(pool_free)} GB")
    if isinstance(quota_gb, int) and quota_gb >= 0 and size > quota_gb:
        warn(f"requested {size} GB exceeds project quota {quota_gb} GB")

    vtypes = safe_list(conn.block_storage.types)
    if type_name is not None:
        return {"size": size, "type": type_name}
    vtype = None
    if vtypes:
        vtype = choose("Volume type", vtypes,
                       label=lambda t: f"{t.name}"
                       + (" [encrypted]" if _is_encrypted_type(conn, t) else ""),
                       allow_none=True, none_label="(default type)")
    return {"size": size, "type": getattr(vtype, "name", None)}
