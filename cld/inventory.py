"""Cross-project read-only inventory snapshot used to annotate menus with usage."""

from cld.cloud import safe_list


def server_az(s):
    """Best-effort availability zone for a server object (or None)."""
    return (getattr(s, "availability_zone", None)
            or (s.to_dict().get("OS-EXT-AZ:availability_zone")
                if hasattr(s, "to_dict") else None))


class Inventory:
    """Fetch all servers once; expose per-AZ / -flavor / -image usage counts."""

    def __init__(self, conn):
        self.conn = conn
        self.servers = safe_list(conn.compute.servers, details=True,
                                 all_projects=True)

    def count_by_az(self):
        counts = {}
        for s in self.servers:
            az = server_az(s) or "(none)"
            counts[az] = counts.get(az, 0) + 1
        return counts

    def count_by_flavor(self):
        counts = {}
        for s in self.servers:
            fl = s.flavor or {}
            key = fl.get("original_name") or fl.get("id") or "(unknown)"
            counts[key] = counts.get(key, 0) + 1
        return counts

    def count_by_image(self):
        counts = {}
        for s in self.servers:
            img = s.image or {}
            key = img.get("id") if isinstance(img, dict) else None
            if key:
                counts[key] = counts.get(key, 0) + 1
        return counts
