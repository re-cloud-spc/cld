# How to create VMs and add storage with `cld`

`cld` is an interactive management tool for Re:Cloud and NanoCloud admins to operate
our Ceph-backed OpenStack cluster. Instead of memorising flavor/image/subnet IDs and
doing quota math by hand, it walks you through each choice and shows the **current
inventory** at every step, then confirms once and acts. Every write is recorded
under `logs/`.

Subcommands:

| command | purpose |
|---------|---------|
| `cld init` | one-time per project: mint an app credential + write its `clouds.yaml` entry |
| `cld list` | read-only inventory (servers, flavors, images, networks, AZs, capacity, clouds) |
| `cld createvm` | provision a VM (root disk only) |
| `cld attachstorage` | create + attach a data volume to an **existing** server |
| `cld check` | authenticate and print the scoped project/user |
| `cld completion` | print a bash completion script (see §9) |

Run as `python3 cld.py <subcommand>` (or `python3 -m cld <subcommand>`). A bare
invocation defaults to `createvm`.

> **Two steps on purpose.** `createvm` provisions the VM and stops; data volumes
> are added separately with `attachstorage`. This keeps a transient Cinder hiccup
> during an attach from ever rolling back the healthy VM (rc3 issue #112).
>
> Files: the `cld/` package (`cli`, `steps`, `vm`, `storage`, `volume`, `cloud`,
> `inventory`, `init`, `audit`, `ui`, `answers`), the `cld.py` entry point,
> `requirements.txt`, `README.md`. (Renamed from `osvm`; no old-name shims remain.)
>
> The `cld/` package now also has `listcmd.py` (the `list` command); its tables are
> the same `cld.steps.render_*` helpers the wizard uses — new wizard steps should
> keep that render/prompt split so `list` gets them for free.

---

## 1. Prerequisites

Already present on the controller nodes: `openstacksdk`, `rich`, `PyYAML`, and the
`openstack` CLI. To run elsewhere:

```bash
pip install -r requirements.txt
```

Optional: `rich-argparse` colours `--help`; without it the help text is the same,
uncoloured.

## 2. One-time setup: credentials (`cld init`, per project)

`cld` authenticates via `clouds.yaml` using **application credentials**. The auth
model is **one application credential per project** — app credentials are
permanently bound to a single project and cannot be re-scoped, so each project you
deploy into becomes its own named cloud entry. You don't hand-write `clouds.yaml`;
`cld init` mints the credential and writes the entry for you.

On a controller node the admin credentials live at `/root/admin-openrc.sh`:

```bash
# Load admin creds into your shell (clears OS_* then exports its own):
source <(sudo cat /root/admin-openrc.sh)

# Create an app credential + clouds.yaml entry for a project (run once per project):
python3 cld.py init --project admin --cloud admin

# For a non-admin project where the admin user has no role yet, add --admin-role
# so the per-project credential can also read cluster-wide inventory:
python3 cld.py init --project tenant-a --admin-role
```

This writes `~/.config/openstack/clouds.yaml` at mode `600` and never prints the
secret. Verify it works (this check ignores any `OS_*` env vars, so it works in
the same shell where you sourced the admin openrc):

```bash
python3 cld.py check --cloud admin
```

Add more projects anytime by re-running `cld init --project <name>`. Revoke a
credential with `openstack application credential delete cld`.

## 3. Inspect inventory (`cld list`)

A read-only view of the cloud — no prompts, no writes, nothing logged. Default
resource is `servers`, scoped to the current project.

```bash
python3 cld.py list                          # servers in the current project
python3 cld.py list servers --all-projects   # every project's servers (admin)
python3 cld.py list volumes                   # Cinder volumes + the VM each is attached to
python3 cld.py list volumes --available       # only available (unattached, attachable) volumes; same as --available=1
python3 cld.py list volumes --available=0     # only the rest (in-use, error, creating, ...)
python3 cld.py list flavors                  # vCPU / RAM / disk + in-use count
python3 cld.py list images
python3 cld.py list networks
python3 cld.py list azs                       # availability zones + compute capacity
python3 cld.py list hosts                     # per-compute-host vCPU/RAM from Placement (admin)
python3 cld.py list capacity                  # Cinder pool capacity + volume quota
python3 cld.py list clouds                    # clouds.yaml entries — no --cloud / auth needed
```

| resource | shows |
|----------|-------|
| `servers` (default) | name, ID, status, flavor, IP, AZ (`+ project` with `--all-projects`) |
| `volumes` | Cinder volumes: size, status, type, bootable, the VM each is attached to, and created/updated (UTC) — `updated` is the last record change (attach/resize/status), not data access. Footer totals attached / unattached / available GB |
| `flavors` | vCPU / RAM / root disk + how many servers use each |
| `images` | visibility, size, min-disk/ram, signed, in-use count |
| `networks` | networks + subnets: CIDR, gateway, kind, port count |
| `azs` | availability zones + per-AZ and per-host compute capacity (admin) |
| `hosts` | per compute host: AZ, service state, servers, vCPU/RAM used/cap/free from Placement (admin) |
| `capacity` | Cinder SDS pool capacity + this project's volume quota |
| `clouds` | configured `clouds.yaml` entries (each = one project); local, no auth |

| flag | effect |
|------|--------|
| `--cloud NAME` | cloud (= project) from clouds.yaml (not needed for `clouds`) |
| `--all-projects` | `servers`/`volumes`: include every project (admin); default is the current project |
| `--available [0\|1]` | `volumes` only: `1` (or a bare `--available`) = only volumes whose status is `available` (unattached, attachable); `0` = the exact complement (`in-use`, `error`, `creating`, …). Omit to show all — the two filters partition the full list. Errors on any other resource |

## 4. Create a VM (`cld createvm`)

```bash
# Recommended first run — walks every step, shows inventory, creates NOTHING:
python3 cld.py createvm --cloud admin --dry-run

# Real run:
python3 cld.py createvm --cloud admin

# Save the choices to replay the same spec later:
python3 cld.py createvm --cloud admin --save-answers myvm.yaml

# Recreate that exact spec without prompts:
python3 cld.py createvm --non-interactive myvm.yaml
```

| flag | effect |
|------|--------|
| `--cloud NAME` | use this cloud (= project) from `clouds.yaml`; otherwise you're prompted |
| `--dry-run` | walk every step, print the create payload, change nothing |
| `--save-answers FILE` | write this run's spec to a YAML file |
| `--non-interactive FILE` | replay a saved spec with no prompts |
| `--host HOST` | pin to this compute host (admin) without the AZ / host questions; an unknown or down host stops before any question, a flavor that doesn't fit needs a yes (default No); overrides a saved spec's host |

Flow: **cloud (project) → availability zone → flavor → compute host (optional pin) → image → network/subnet →
security → data disks (optional, planned only) → name → confirm → create** (then an
optional floating IP). When the VM is ACTIVE, `cld` prints one exact
`attachstorage --disk ... --disk ...` command for the planned disks (it attaches and
sets them up inside the VM), or the generic `attachstorage` command if none were planned.

## 5. Add a data volume (`cld attachstorage`)

```bash
# Interactive — lists the project's servers, then shows capacity/quota:
python3 cld.py attachstorage --cloud admin

# Target a specific server, size and type inline:
python3 cld.py attachstorage --cloud admin --serverid 3f1c8d2a-... --size 50 --type encrypted

# Attach EXISTING volumes (instead of creating one), then set them up inside the VM:
python3 cld.py attachstorage --cloud admin --serverid 3f1c8d2a-... --disk dedf1d0a-... --disk 7c2e19b4-...

# See what would happen without creating/attaching anything:
python3 cld.py attachstorage --cloud admin --serverid 3f1c8d2a-... --size 50 --dry-run
```

| flag | effect |
|------|--------|
| `--cloud NAME` | cloud (= project) the server lives in |
| `--serverid ID` | target server ID (otherwise lists the project's servers to pick) |
| `--disk VOLUME_ID` | attach an EXISTING volume instead of creating one; repeat for several. Only if it's `available`, unattached and in this project (one already attached to *this* server gets the in-guest step only). An old root disk is attached only to be wiped, after you type its ID |
| `--attach-only` | attach only; skip the in-guest step and print the manual mount steps |
| `--size GB` | volume size for a new volume (not allowed with `--disk`) |
| `--type TYPE` | volume type, e.g. an encrypted/LUKS type (not allowed with `--disk`) |
| `--dry-run` | show what would be created/attached, change nothing |

Before you commit it shows **true SDS capacity** (Cinder pool total / free /
allocated / over-subscription) and your project's volume quota vs usage, and
validates the size against both. The disk model: a VM boots onto the flavor's small
Ceph root disk; extra capacity comes from these attached Cinder (Ceph RBD) volumes.

### Inside the VM: partition, format, mount

After the attach, `cld` goes **into the VM** and sets the disk up with you:

1. **Access.** First the **qemu-guest-agent**, reached over SSH through the compute
   node that hosts the VM (`docker exec nova_libvirt virsh qemu-agent-command`). This
   needs your SSH key on that compute node and a running agent in the guest, but no
   key inside the VM and no network route to it. If that fails, `cld` asks for an
   **SSH target** (`user@address`, passwordless sudo). If neither works, it prints
   the manual steps below. Today rc3-c-1 and rc3-c-2 work; rc3-c-3 does not accept
   the key.
2. **Find the disk by its serial**, `/dev/disk/by-id/virtio-<first 20 chars of the
   volume ID>`, never by Nova's `/dev/vdX` guess. It is classified as *blank*, *has
   data*, *boot clash* (shares a label/UUID the VM boots by), *mounted* or *root*.
   Mounted and root disks are never touched.
3. **Interview**, nothing changed yet:
   - **blank or to be wiped**: use the **whole disk** (one filesystem, no partition
     table) or **split it** (GPT; you give each partition's size, the last one gets
     the rest). Then, for each filesystem: ext4 or xfs, mount point (under `/mnt`,
     `/srv`, `/data` or `/opt`; must be free and empty), who may write (root, everyone
     with sticky `1777`, or one user), and fstab yes/no. Or **move an existing
     folder onto it** (see below).
   - **has data**: keep it and choose which partition mounts where (**read-only by
     default**), wipe it (type its ID), or leave it unmounted.
4. **Plan and apply.** It shows the plan, re-checks everything (same device, not
   mounted, still blank, mount points still free) and asks **"Apply this plan?"
   (default No)**. Then it wipes / partitions / formats / mounts. fstab is backed up to
   `/etc/fstab.bak-cld-<time>`, only **appended to** (by-id path + `nofail`, never
   `UUID=`, since cloned volumes share UUIDs), and checked with `findmnt --verify`; a
   failed check restores the backup. Every step is written to the audit log
   (`guest.*`).

**Old root disks are wiped, never reused as they are.** A bootable or image-derived
volume, or one whose partitions carry boot labels (`cloudimg-rootfs`, `UEFI`, `BOOT`,
...), can make the VM boot from it after a reboot: udev points
`/dev/disk/by-label/*` at whichever disk it saw last. Before attaching one, `cld` shows
what is on it (a read-only Ceph look) and attaches it **only after you type its ID** to
agree that it is wiped right after. Say no and it is not attached. Without guest access
it is never attached. If the wipe does not happen after all, `cld` says so and prints
the detach command: don't reboot until it is detached or wiped.

#### Moving an existing folder onto the new disk

Instead of a new, empty folder, a new filesystem can take over an existing folder:

| Folder | How |
|---|---|
| `/home`, `/home/<user>`, `/var/lib/<name>` (docker, postgresql, mysql, ...), `/var/tmp`, `/var/www`, folders under `/srv` `/opt` `/data` | copied while nothing uses it, then switched |
| `/var/log` | the same; journald is told to let go of `/var/log/journal` and rsyslog re-opens its files afterwards |
| `/tmp` | not copied: the filesystem is prepared (`1777`, `nosuid,nodev`) and **switches at the next reboot** (running programs keep sockets in `/tmp`) |
| all of `/var`, `/`, `/boot`, `/etc`, `/usr` | refused |

The folder must be a plain directory (not a symlink, not a mount point such as a
tmpfs `/tmp`), with no mounts below it, and use at most 85% of the new filesystem.
**Nothing may be using it**: `cld` lists the processes (open files, working
directories, mapped libraries) and unix sockets under it and stops if there are any,
at the interview and again right before the switch. Log the user out or
`systemctl stop <service>` first (docker: `systemctl stop docker docker.socket`).

The move, after "Apply this plan?":

1. mount the new filesystem at `/mnt/.cld-move-<label>` and copy (`rsync -aHAXx`, or
   `cp -ax` without rsync: owners, modes, hard links, ACLs and xattrs kept);
2. compare entries and bytes on both sides; a mismatch stops here, nothing switched;
3. in one step: rename the folder to `<folder>.cld-old-<time>`, recreate it with the
   same owner/mode, mount the new filesystem on it (a failure puts the original
   back); then `restorecon` on SELinux systems;
4. add the fstab line (always, for a moved folder).

**The old copy is never deleted by `cld`.** It stays on the VM's own disk as
`<folder>.cld-old-<time>`; check the moved folder, start the services again, then
delete it yourself. The fstab line has `nofail`: if the volume is missing at boot,
the VM still boots and the folder is **empty** (not stale), which services notice.

### Mounting by hand (no guest access, or `--attach-only`)

If `cld` cannot get into the guest, or you pass `--attach-only`, the disk is only a
**block device** in the VM: nothing is mounted. `cld` then prints the device and the
steps to run **inside the VM** (as root).

The device is printed as its **exact, stable path**
`/dev/disk/by-id/virtio-<first 20 chars of the volume ID>`. Nova sets the virtio disk
serial to the Cinder volume ID, so this path always names *this* volume. The
`/dev/vdX` that Nova reports is only the name it requested: the guest kernel names disks
in discovery order, so it can differ and can change across reboots. `cld` still shows it
as a hint. For volume `3f31a7d0-0e05-4e83-b42b-edf0aa8f9d7a`:

```bash
lsblk -f /dev/disk/by-id/virtio-3f31a7d0-0e05-4e83-b        # existing partitions/filesystem?
sudo wipefs -n /dev/disk/by-id/virtio-3f31a7d0-0e05-4e83-b  # no output = blank
sudo mkfs.ext4 -L data-3f31a7d0 /dev/disk/by-id/virtio-3f31a7d0-0e05-4e83-b   # ONLY if blank — ERASES the disk
sudo mkdir -p /mnt/data
sudo mount /dev/disk/by-id/virtio-3f31a7d0-0e05-4e83-b /mnt/data
echo "/dev/disk/by-id/virtio-3f31a7d0-0e05-4e83-b /mnt/data ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2" | sudo tee -a /etc/fstab
sudo findmnt --verify                                       # check the fstab entry
```

The fstab entry uses the by-id path rather than `UUID=`, because volumes cloned from
the same image share filesystem UUIDs. `nofail` keeps a later detach from blocking boot.

> Only run `mkfs` if the disk is blank — a re-attached data volume already has a
> filesystem and must **not** be reformatted.

**Image/root volumes can hijack the boot**, see above. Attached by hand, don't reboot
until `ls -l /dev/disk/by-label/` points only at the VM's own disk: reformat the volume
(if its data is disposable) or detach it.

## 5b. Delete an unattached volume (`cld deletevolume`)

```bash
python3 cld.py deletevolume --cloud admin --volumeid bd5407da-dd18-48dc-80e7-45e189e724c3 --dry-run   # report only
python3 cld.py deletevolume --cloud admin --volumeid bd5407da-dd18-48dc-80e7-45e189e724c3             # report, then decide
```

| flag | effect |
|------|--------|
| `--cloud NAME` | cloud (= project) the volume lives in; only volumes of that project can be deleted |
| `--volumeid UUID` | **required**. Full UUID of the ONE volume to delete (names are not unique, so they're refused). Refused if attached, in any status other than `available`/`error`, in another project, or if it has snapshots |
| `--dry-run` | show the full report (facts, contents, age, history) and stop; delete nothing |

`deletevolume` permanently deletes **one** volume, and only after showing you what
you're about to lose. Pass the full UUID; names aren't unique, so `cld` won't guess.

**It refuses, with no override, when:**
- the volume is **attached**: it has Cinder attachments, a Nova server still lists
  it, or a client holds its Ceph image open (an RBD watcher). `cld` prints the
  detach procedure to follow first: stop use and `umount` inside the VM, remove its
  `/etc/fstab` line, `openstack server remove volume <server> <volume>`, then re-run.
  `cld` never detaches anything itself.
- the status is anything other than `available` or `error`. `reserved` (a stale
  attach reservation), `attaching`, `detaching` and `error_deleting` each get a hint
  on what a human must check.
- the volume belongs to another project than the credential's.
- the volume has **Cinder snapshots**. `cld` never cascades.

**Otherwise it reports:**
- **Cinder facts:** size, type, bootable, source (image, snapshot or volume), age,
  last record change, metadata, snapshots, backups, and clones made from it.
- **Contents**, read-only from the Ceph image: bytes ever written ("never written"
  means it's empty); the partition table; each filesystem's type, label and used
  space; and for ext4, when and where it was last mounted and its lifetime writes.
  It never mounts or maps anything. It needs the Ceph admin keyring, used directly
  as root or via `sudo -n`; without that access it says so, and you decide on
  metadata alone.
- **History:** the RBD created, modified and accessed timestamps, plus every `cld`
  audit-log line that mentions the volume.

**Then you decide.** Answer `y` (the default is No; Enter, `q` or Ctrl-D keep the
volume), then type the first 8 characters of the volume ID. `cld` re-reads the volume
and refuses if anything changed while you were deciding. It then deletes without
force, writes a `volume.delete` audit line, and polls Cinder until the volume is
confirmed gone (or reports `error_deleting`). Any backups remain.

## 6. What each createvm step shows you

Menus are numbered from `0`; multi-selects take e.g. `0,2`. Where a step can be
skipped, skipping is Enter only (it has no number). `q` or Ctrl-D quits.

- **Cloud (project)** — the project is fixed by the chosen credential entry.
- **Availability zone** (only prompts if more than one; `internal` is never
  offered) — per-AZ host count, vCPU and RAM used/total and RAM free summed from
  Placement, server count, the per-host table, plus the Cinder and Neutron AZ lists.
- **Flavor** — vCPU / RAM / root disk (the small Ceph-backed boot disk) and how
  many existing servers use each flavor.
- **Compute host** (admin) — each compute host in the chosen AZ with free vCPU/RAM
  from Placement, the RAM left after this flavor, and whether it fits. Enter lets
  the scheduler decide (default). Picking a host sends Nova's 2.74 `host` field: the
  scheduler and Placement still check it, so a full host is refused, not
  overcommitted. A replayed `--non-interactive` spec re-checks its saved host first
  (a real run on a host that no longer fits needs an explicit yes). No round-robin:
  the scheduler's RAM weigher already prefers the emptiest host.
- **Image** — size, min-disk / min-ram, visibility, whether it's signed, and how
  many servers were booted from each (boot-from-volume servers aren't counted).
- **Data disks** (after security; optional) — this project's `available`, unattached
  volumes with a short ID (last 4 characters, longer if that would be ambiguous),
  name, size, bootable flag, AZ and created date. Pick by the `#`
  (e.g. `0,2`; Enter = none). A bootable / image volume (likely an old root disk,
  whose `cloudimg-rootfs`/`UEFI`/`BOOT` labels can hijack the new VM's boot) needs an
  explicit yes each, default No, and `attachstorage` will wipe it. **Nothing is attached by `createvm`**: the disks are
  saved in the spec, and once the VM is ACTIVE (or in `--dry-run`) each is re-read
  and the disks still attachable get one `cld attachstorage --serverid <vm> --disk <uuid> --disk ...`
  command; a disk that is no longer available, or belongs to another project, is left out.
- **Network / subnet** — every usable network and its subnets with CIDR, gateway,
  and port count; external (publicly exposed) networks are flagged. The wizard then
  **assigns the next available static internal IP** for the chosen subnet (lowest
  free IPv4 in the allocation pool, skipping the gateway and in-use addresses) and
  reserves it via a dedicated Neutron port at create time. IPv6 / exhausted subnets
  fall back to Neutron auto-assignment.
- **Security** — see below.
- **Confirm** — a single summary screen of every choice before anything is created.

## 7. Security behaviour (built in)

- Prompts for one or more **SSH keypairs** (comma-separated multi-select, e.g.
  `0,2`); warns if you skip them. Nova injects only one keypair at boot, so the
  first selected becomes the VM's keypair and the rest are added via cloud-init
  `user_data` (requires the image to support cloud-init).
- Lists **security groups** and loudly flags any with world-open (`0.0.0.0/0`)
  ingress; adding such a group requires explicit confirmation.
  If you pick none, cld reads the project's own `default` group and reports its
  actual rules; a world-open `default` needs the same explicit yes (default No
  returns to the group menu).
- **Floating IP** assignment is off by default and needs a second confirmation;
  choosing an external network warns and re-confirms.
- Offers an **encrypted (LUKS) volume type** for the data volume when available.
- Warns on **community** images.
- Flags **flavors whose NUMA/hugepage extra specs cannot be satisfied** — the
  `topology` column marks them `!` (e.g. `hw:numa_nodes=3` on a 4 vCPU / 8192 MB
  flavor, which Nova rejects with *"Instance CPUs and/or memory cannot be evenly
  distributed across instance NUMA nodes"*). Selecting one requires explicit
  confirmation, so it fails before a port or server is created; `hw:mem_page_size`
  is surfaced as a warning since it needs hugepages reserved on the compute.
- All inventory reads are **read-only**; the only writes are the explicit
  create/attach/delete actions, each gated by a final confirmation.
- `deletevolume` refuses attached, transitional, cross-project and snapshotted
  volumes. A delete needs a `y` (default No) plus the typed ID prefix.
- **Rollback defaults to keeping resources** — on a mid-create failure the rollback
  prompt defaults to *No*, so Enter leaves everything for inspection.
  `attachstorage` rollback only ever deletes the dangling volume, never the server.
- **A failed attach reports the volume's re-read state**, not the state it had before
  the call: `available` (safe to re-run), `reserved`/`attaching` (Nova reserved it —
  clear the attachment by hand first), `in-use` (the attach may have succeeded), or
  UNKNOWN if the volume could not be re-read. `cld` never retries automatically, and
  never offers to delete a volume Nova may have reserved or attached.
- Every write action is appended to `logs/cld-<YYYYMMDD>.log` with resource IDs.

## 8. Troubleshooting

- **"Application credentials cannot request a scope." (HTTP 401)** — this comes
  from the **raw `openstack` CLI**, not from `cld`. You're in the shell where you
  ran `source <(sudo cat /root/admin-openrc.sh)`, so the `OS_*` env vars are
  layered on top of the cloud entry and force a project scope that app credentials
  reject. `cld` ignores `OS_*` entirely (it passes `load_envvars=False`), so the
  fix is to verify with the tool instead:
  ```bash
  python3 cld.py check --cloud admin
  ```
  If you specifically want the raw CLI, clear the vars first (a subshell keeps
  your session intact):
  ```bash
  ( for v in $(env | sed -n 's/^\(OS_[A-Z_]*\)=.*/\1/p'); do unset "$v"; done
    openstack --os-cloud admin token issue )
  ```
- **"No usable clouds.yaml was found."** — run the step 2 setup; you haven't
  created a credential entry yet.
- **Inventory tables are empty or show only your project** (AZ capacity, Cinder
  pool stats, all-project server counts) — these need the **admin role** on the
  credential's project. Re-create the entry with `--admin-role`, or grant it:
  `openstack role add --user admin --project <proj> admin`.
- **Want to deploy into a different project** — pick another cloud entry, or add
  one with `cld init --project <name>`. A single credential can only ever create
  resources in its own project.
- **No SSH keypair offered** — create one first:
  `openstack keypair create <name> > <name>.pem`.

## 9. Notes / limitations

- **Shell completion (bash):** `python3 cld.py completion bash > ~/.local/share/cld/completion.bash`,
  then `source` that file from `~/.bashrc`. It completes subcommands, flags, `list`
  resources, `--available`, `--cloud` names and file paths, for `cld`, `./cld.py`,
  `python3 cld.py` and `python3 -m cld`. Regenerate it after a cld update that adds flags.

- The VM boots from the image onto the flavor's small Ceph/RBD root disk; extra
  capacity comes from `attachstorage` volumes, not the root disk.
- Host→AZ mapping comes from Nova's AZ listing; per-host capacity from Placement
  (hypervisor stats are `None` at this Nova microversion).
- Optional raw-Ceph cross-check (needs Ceph admin on a host, not the OpenStack
  API): `ceph df` shows pool usage behind the Cinder figures.

## 10. See also

- **Backing up and recovering VMs and volumes** —
  [BACKUP_RECOVERY.md](BACKUP_RECOVERY.md): Cinder snapshots vs off-pool backups,
  server snapshots, full-VM-consistent sets, recovery recipes, and scheduled backups
  with retention. (`cld` does not perform backups; these are operator procedures.)
