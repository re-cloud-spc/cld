"""Read-only peek at the raw Ceph RBD image behind a Cinder volume.

Answers "what is actually ON this volume?" without attaching, mapping or mounting
anything: the image is opened with read_only=True through librbd and the partition
table and filesystem superblocks are parsed from raw bytes. Nothing on the host
changes (no /dev/rbdN, no mount) and nothing is written to the image -- not even
its access timestamp (rbd_atime_update_interval=0 for this client).

Needs the Ceph admin keyring (root-only on the controller). `peek()` tries
in-process first and, if that is refused, re-runs THIS FILE as root via
`sudo -n` (never prompts; if sudo would need a password it just reports that the
contents could not be inspected). Standalone use -- prints JSON:

    sudo python3 cld/rbdpeek.py <volume-uuid>

Keep this file self-contained (stdlib + rados/rbd only): it runs outside the
package when invoked through sudo.
"""

import datetime
import json
import os
import re
import struct
import subprocess
import sys

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CEPH_CONF = "/etc/ceph/ceph.conf"
_SECTOR = 512
_PROBE = 70 * 1024            # covers every superblock offset probed below


def _ts(t):
    """datetime/epoch -> ISO string (UTC), or None for unset (0) values."""
    if t is None or t == 0:
        return None
    if isinstance(t, (int, float)):
        t = datetime.datetime.fromtimestamp(t, datetime.timezone.utc)
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _cstr(b):
    return b.split(b"\0", 1)[0].decode("utf-8", "replace").strip()


def _uuid(b):
    h = b.hex()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# --------------------------------------------------------------------------- #
# filesystem / signature probes (raw bytes -> dict)
# --------------------------------------------------------------------------- #
def _probe_fs(buf):
    """Identify what starts at the beginning of `buf`. Returns a dict or None."""
    sb = buf[1024:2048]
    if len(sb) >= 1024 and struct.unpack_from("<H", sb, 56)[0] == 0xEF53:
        bs = 1024 << struct.unpack_from("<I", sb, 24)[0]
        blocks = struct.unpack_from("<I", sb, 4)[0]
        free = struct.unpack_from("<I", sb, 12)[0]
        if struct.unpack_from("<I", sb, 96)[0] & 0x80:        # INCOMPAT_64BIT
            blocks |= struct.unpack_from("<I", sb, 0x150)[0] << 32
            free |= struct.unpack_from("<I", sb, 0x158)[0] << 32
        return {
            "type": "ext4/3/2",
            "label": _cstr(sb[120:136]),
            "uuid": _uuid(sb[104:120]),
            "size_bytes": blocks * bs,
            "used_bytes": (blocks - free) * bs,
            "created": _ts(struct.unpack_from("<I", sb, 0x108)[0]),
            "last_mounted": _ts(struct.unpack_from("<I", sb, 44)[0]),
            "last_mounted_on": _cstr(sb[136:200]) or None,
            "last_written": _ts(struct.unpack_from("<I", sb, 48)[0]),
            "mount_count": struct.unpack_from("<H", sb, 52)[0],
            "lifetime_written_bytes": struct.unpack_from("<Q", sb, 0x178)[0] * 1024,
        }
    if buf[:4] == b"XFSB":
        bs = struct.unpack_from(">I", buf, 4)[0]
        blocks = struct.unpack_from(">Q", buf, 8)[0]
        free = struct.unpack_from(">Q", buf, 0x90)[0]
        return {"type": "xfs", "label": _cstr(buf[108:120]), "uuid": _uuid(buf[32:48]),
                "size_bytes": blocks * bs, "used_bytes": (blocks - free) * bs}
    if buf[:6] == b"LUKS\xba\xbe":
        return {"type": "LUKS (encrypted -- contents unreadable)",
                "uuid": _cstr(buf[168:208])}
    if buf[512:520] == b"LABELONE":
        return {"type": "LVM physical volume"}
    if buf[4086:4096] == b"SWAPSPACE2":
        return {"type": "swap", "label": _cstr(buf[1024 + 28:1024 + 44])}
    if buf[3:11] == b"NTFS    ":
        return {"type": "ntfs"}
    if buf[0x10040:0x10048] == b"_BHRfS_M":
        return {"type": "btrfs", "label": _cstr(buf[0x1012B:0x1022B])}
    if buf[0x52:0x57] == b"FAT32":
        return {"type": "vfat (FAT32)", "label": _cstr(buf[0x47:0x52])}
    if buf[0x36:0x39] == b"FAT":
        return {"type": "vfat", "label": _cstr(buf[0x2B:0x36])}
    if not buf.strip(b"\0"):
        return {"type": "(zeros -- nothing written here)"}
    return {"type": "(unrecognised data)"}


def _partitions(img):
    """[(name, offset, size, part_label)] from GPT or MBR; [] for a bare disk."""
    head = img.read(0, 34 * _SECTOR)
    parts = []
    if head[_SECTOR:_SECTOR + 8] == b"EFI PART":
        entries_lba, n, esz = struct.unpack_from("<QII", head, _SECTOR + 72)
        raw = img.read(entries_lba * _SECTOR, min(n, 256) * esz)
        for i in range(min(n, 256)):
            e = raw[i * esz:(i + 1) * esz]
            if len(e) < 128 or not e[:16].strip(b"\0"):
                continue
            first, last = struct.unpack_from("<QQ", e, 32)
            name = e[56:128].decode("utf-16-le", "replace").split("\0", 1)[0]
            parts.append((f"p{i + 1}", first * _SECTOR, (last - first + 1) * _SECTOR, name))
        return "gpt", parts
    if head[510:512] == b"\x55\xaa":
        for i in range(4):
            e = head[446 + 16 * i:462 + 16 * i]
            ptype = e[4]
            start, count = struct.unpack_from("<II", e, 8)
            if ptype and count and ptype != 0xEE:
                parts.append((f"p{i + 1}", start * _SECTOR, count * _SECTOR, f"type 0x{ptype:02x}"))
        if parts:
            return "mbr", parts
    return None, []


# --------------------------------------------------------------------------- #
# image-level facts
# --------------------------------------------------------------------------- #
def _allocated_bytes(img, size):
    total = [0]

    def cb(offset, length, exists):
        if exists:
            total[0] += length
    try:
        img.diff_iterate(0, size, None, cb, whole_object=True)
    except TypeError:                       # older bindings: no whole_object kwarg
        img.diff_iterate(0, size, None, cb)
    return total[0]


def _try(fn, default=None):
    try:
        return fn()
    except Exception:  # noqa: BLE001 - optional facts vary by Ceph release
        return default


def _inspect(volume_id):
    import rados
    import rbd

    cluster = rados.Rados(conffile=CEPH_CONF)
    # Opening an image -- even read_only -- bumps its access timestamp, which would
    # destroy the very "last accessed" history this peek reports. 0 disables it for
    # this client only (librbd option; the cluster config is untouched).
    cluster.conf_set("rbd_atime_update_interval", "0")
    cluster.conf_set("rbd_mtime_update_interval", "0")
    cluster.connect(timeout=15)
    try:
        hits = []
        for pool in cluster.list_pools():
            if pool.startswith("."):
                continue
            ioctx = cluster.open_ioctx(pool)
            try:
                img = rbd.Image(ioctx, f"volume-{volume_id}", read_only=True)
            except Exception:  # noqa: BLE001 - not in this pool / not an rbd pool
                ioctx.close()
                continue
            hits.append((pool, ioctx, img))
        if not hits:
            return {"found": False}
        if len(hits) > 1:
            for _, io, im in hits:
                im.close(); io.close()
            return {"found": False,
                    "error": f"image found in several pools: {[h[0] for h in hits]}"}
        pool, ioctx, img = hits[0]
        try:
            size = img.size()
            res = {
                "found": True,
                "pool": pool,
                "image": f"volume-{volume_id}",
                "size_bytes": size,
                "allocated_bytes": _try(lambda: _allocated_bytes(img, size)),
                "rbd_created": _ts(_try(img.create_timestamp)),
                "rbd_modified": _ts(_try(img.modify_timestamp)),
                "rbd_accessed": _ts(_try(img.access_timestamp)),
                "snapshots": [s["name"] for s in _try(lambda: list(img.list_snaps()), [])],
                "children": [f"{c.get('pool', c.get('pool_name', '?'))}/{c.get('image', c.get('image_name', '?'))}"
                             for c in _try(lambda: list(img.list_children2()), [])],
                "parent": _try(lambda: "{pool_name}/{image_name}@{snap_name}".format(
                    **img.get_parent_image_spec())),
                "watchers": [w.get("addr") for w in _try(lambda: list(img.watchers_list()), [])],
            }
            table, parts = _partitions(img)
            res["partition_table"] = table
            regions = parts or [("disk", 0, size, None)]
            res["regions"] = []
            for name, off, psize, plabel in regions:
                fs = _probe_fs(img.read(off, min(_PROBE, psize)))
                res["regions"].append({"name": name, "offset": off, "size_bytes": psize,
                                       "part_label": plabel, "fs": fs})
            return res
        finally:
            img.close()
            ioctx.close()
    finally:
        cluster.shutdown()


def peek(volume_id, timeout=180):
    """-> dict. Never raises: failures come back as {"found": False, "error": ...}."""
    if not UUID_RE.match(volume_id or ""):
        return {"found": False, "error": "not a volume UUID"}
    try:
        return _inspect(volume_id)
    except ImportError:
        return {"found": False, "error": "python3-rados/python3-rbd not installed"}
    except Exception as e:  # noqa: BLE001 - usually: keyring is root-only
        if os.geteuid() == 0:
            return {"found": False, "error": f"Ceph read failed: {e}"}
        first = e
    try:  # non-root: same read-only code, as root, never prompting for a password
        r = subprocess.run(["sudo", "-n", sys.executable, os.path.abspath(__file__),
                            volume_id], capture_output=True, text=True, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return {"found": False, "error": f"Ceph read failed ({first}); sudo: {e}"}
    if r.returncode != 0:
        return {"found": False,
                "error": f"no Ceph access as this user ({first}) and sudo -n refused: "
                         f"{(r.stderr or '').strip().splitlines()[-1:] or ''}"}
    try:
        return json.loads(r.stdout)
    except ValueError:
        return {"found": False, "error": "unreadable output from privileged peek"}


if __name__ == "__main__":
    if len(sys.argv) != 2 or not UUID_RE.match(sys.argv[1]):
        sys.exit("usage: rbdpeek.py <volume-uuid>")
    try:
        print(json.dumps(_inspect(sys.argv[1])))
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"found": False, "error": f"Ceph read failed: {e}"}))
