# cld — Interactive Management Tool for Re:Cloud and NanoCloud Admins

A small, growing CLI for Re:Cloud and NanoCloud admins to operate the local
(Ceph-backed) OpenStack cluster. It shows the **current inventory** at every step
so you choose with real capacity in view, confirms once, then acts. Every write
action is recorded to `logs/`.

Subcommands:

| command | purpose |
|---------|---------|
| `cld init` | one-time per project: mint an application credential + write its `clouds.yaml` entry |
| `cld list` | show read-only inventory (servers, flavors, images, networks, AZs, capacity, clouds) |
| `cld createvm` | provision a VM (boot from image onto the flavor's small Ceph root disk) |
| `cld attachstorage` | create + attach a Cinder data volume to an **existing** server |
| `cld deletevolume` | permanently delete ONE **unattached** volume, after a report of its contents, age and history |
| `cld check` | authenticate, print the scoped project/user, and exit |
| `cld completion` | print a bash completion script (see [Shell completion](#shell-completion-bash)) |

> **VM creation and data-volume attachment are deliberately two separate steps.**
> `createvm` never creates or attaches volumes (it can *plan* existing disks and
> print their `attachstorage --disk` commands); afterwards you run `attachstorage`
> against the running server. Beyond being composable, this is a safety property: a transient
> Cinder failure during attach can only ever roll back the *volume*, never the
> healthy VM (rc3 issue #112).

Run it as `python3 cld.py <subcommand>` or `python3 -m cld <subcommand>`. A bare
invocation defaults to `createvm` (so `cld --dry-run` == `cld createvm --dry-run`).

## Requirements

Already present on this host: `openstacksdk` 3.0.0, `rich`, `PyYAML`, and the
`openstack` CLI 6.6.0. To reproduce elsewhere:

```bash
pip install -r requirements.txt
```

Optional: `rich-argparse` colours `--help` (not packaged for Ubuntu; for one user:
`pip install --user --no-deps --break-system-packages rich-argparse`). Without it
`--help` is the same text, uncoloured.

## One-time setup: credentials (`cld init`)

This host (`rc3-x-3`) is a **Kolla-Ansible OpenStack controller**, so the admin
credentials already exist at `/root/admin-openrc.sh`. You don't hand-write
`clouds.yaml` — `cld init` mints an **application credential** and writes the
entry for you.

The auth model is **one application credential per project**: app credentials are
permanently bound to a single project (they cannot be re-scoped), so each project
you deploy into becomes its own named cloud entry. `createvm`'s first step is then
"pick the cloud (= project)".

```bash
# 1. Load admin creds into your shell (admin-openrc clears OS_* then exports its own):
source <(sudo cat /root/admin-openrc.sh)

# 2. Create an app credential + clouds.yaml entry for a project (run once per project):
python3 cld.py init --project admin --cloud admin

# For a non-admin project, if the admin user has no role there, add --admin-role
# so the per-project credential can still read cluster-wide inventory:
python3 cld.py init --project tenant-a --admin-role
```

`cld init` reuses the values from your environment (`OS_AUTH_URL`
`http://192.168.1.100:5000`, `OS_REGION_NAME` `RegionOne`, `OS_INTERFACE`
`internal`), writes `~/.config/openstack/clouds.yaml` at mode `600`, and never
prints the credential secret. Add more projects anytime by re-running it.

> The `admin` role caveat: cross-project inventory (hypervisor capacity, Cinder
> pool stats, all-projects server counts) needs the admin role on the credential's
> project. Without it the tool still works but those tables show only your own
> project or appear empty.

> **Gotcha — "Application credentials cannot request a scope" (HTTP 401):** that
> error comes from the raw `openstack` CLI, not `cld`. The shell where you sourced
> `admin-openrc.sh` still has `OS_*` vars that clash with app-credential auth.
> `cld` ignores `OS_*` (it passes `load_envvars=False`), so verify with the tool
> instead — works in any shell:
> ```bash
> python3 cld.py check --cloud admin
> ```

To revoke a credential later:

```bash
openstack application credential delete cld
```

## Inspect inventory (`cld list`)

A read-only view of the cloud — no prompts, no writes, nothing logged.

```bash
python3 cld.py list                          # servers in the current project (default)
python3 cld.py list servers --all-projects   # every project's servers (admin)
python3 cld.py list volumes                  # Cinder volumes + the VM each is attached to
python3 cld.py list volumes --available      # only available (unattached, attachable) volumes; same as --available=1
python3 cld.py list volumes --available=0    # only the rest (in-use, error, creating, ...)
python3 cld.py list flavors                  # vCPU / RAM / disk + in-use count
python3 cld.py list hosts                    # per-compute-host vCPU/RAM from Placement (admin)
python3 cld.py list capacity                 # Cinder pool capacity + volume quota
python3 cld.py list clouds                   # configured clouds.yaml entries (no auth needed)
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

## Usage

```bash
# Walk the whole createvm wizard but create nothing (recommended first run):
python3 cld.py createvm --cloud admin --dry-run

# Real VM create, saving the choices for later:
python3 cld.py createvm --cloud admin --save-answers myvm.yaml

# Recreate the same VM spec without prompts:
python3 cld.py createvm --non-interactive myvm.yaml

# Add a data volume to an existing server (interactive picks the server):
python3 cld.py attachstorage --cloud admin --serverid 3f1c8d2a-...

# Non-interactive volume add:
python3 cld.py attachstorage --cloud admin --serverid 3f1c8d2a-... --size 50 --type encrypted

# Attach EXISTING volumes (instead of creating one), then set them up inside the VM:
python3 cld.py attachstorage --cloud admin --serverid 3f1c8d2a-... --disk dedf1d0a-... --disk 7c2e19b4-...
```

### `createvm` flags

| flag | effect |
|------|--------|
| `--cloud NAME` | use this named cloud (= project) from clouds.yaml (otherwise prompts) |
| `--dry-run` | walk every step, print the create payload, change nothing |
| `--save-answers FILE` | write this run's spec to a YAML file |
| `--non-interactive FILE` | replay a saved spec with no prompts |
| `--host HOST` | pin to this compute host (admin) without the AZ / host questions; an unknown or down host stops before any question, a flavor that doesn't fit needs a yes (default No); overrides a saved spec's host |

### `attachstorage` flags

| flag | effect |
|------|--------|
| `--cloud NAME` | cloud (= project) the server lives in |
| `--serverid ID` | target server ID (otherwise lists the project's servers to pick) |
| `--disk VOLUME_ID` | attach an EXISTING volume instead of creating one; repeat for several. Only if it's `available`, unattached and in this project (one already attached to *this* server gets the in-guest step only). An old root disk is attached only to be wiped, after you type its ID |
| `--attach-only` | attach only; skip the in-guest step and print the manual mount steps |
| `--size GB` | volume size for a new volume (not allowed with `--disk`) |
| `--type TYPE` | volume type, e.g. an encrypted/LUKS type (not allowed with `--disk`) |
| `--dry-run` | show what would be created/attached, change nothing |

After the attach, `cld` goes **into the VM** (qemu-guest-agent via the compute node,
or SSH as a fallback), finds the disk by its serial and asks you how to lay it out:
whole disk or split into partitions, filesystem, mount point and writers for each,
fstab. A filesystem can also **take over an existing folder** (`/home`, `/home/<user>`,
`/var/log`, `/var/lib/<name>`, `/tmp`, ...: copied, compared, old copy kept; never all
of `/var`). For a disk with data: which partition mounts where (read-only by default),
wipe, or leave it. It shows the plan and asks (default No) before changing anything; fstab is
backed up, only appended to and verified. **Old root disks** (bootable, from an image,
or with boot labels) are attached only to be **wiped**, after you type the volume ID.
Without guest access it prints the manual steps. See
[docs/USAGE.md](docs/USAGE.md#inside-the-vm-partition-format-mount).

## Delete an unattached volume (`cld deletevolume`)

```bash
python3 cld.py deletevolume --cloud admin --volumeid bd5407da-dd18-48dc-80e7-45e189e724c3 --dry-run   # report only
python3 cld.py deletevolume --cloud admin --volumeid bd5407da-dd18-48dc-80e7-45e189e724c3             # report, then decide
```

### `deletevolume` flags

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

## Shell completion (bash)

`cld completion bash` prints a completion script generated from cld's own argument
parser: subcommands, each subcommand's flags, `list` resources, `--available 0|1`,
`--cloud` names from `clouds.yaml` (entry names only, read at TAB time), and file
paths for `--save-answers` / `--non-interactive`. IDs, sizes and types are not
completed (that would need API calls). It works for `cld`, `./cld.py`,
`python3 cld.py` and `python3 -m cld`; ordinary `python3 <TAB>` keeps its usual
completion. Local only: no connection, nothing logged.

```bash
mkdir -p ~/.local/share/cld
python3 cld.py completion bash > ~/.local/share/cld/completion.bash
echo 'source ~/.local/share/cld/completion.bash' >> ~/.bashrc   # once
```

Regenerate the file after pulling a cld change that adds flags or subcommands.

## Backup & recovery

`cld` does not perform backups; see the operator runbook
[docs/BACKUP_RECOVERY.md](docs/BACKUP_RECOVERY.md) for how to back up and recover VMs
and volumes (Cinder snapshots vs off-pool backups, server snapshots, full-VM
consistency, on-demand and scheduled+retention). Two things to internalise first: a
Cinder *snapshot* lives in the same Ceph pool as its source (a rollback point, **not**
disaster recovery), and online backups are **crash-consistent** because `cld` has no
in-guest access.

## What "inventory" means at each step

Menus are numbered from `0`; multi-selects take e.g. `0,2`. Where a step can be
skipped, skipping is Enter only (it has no number). `q` or Ctrl-D quits.

- **Project** — fixed by the chosen credential; the tool re-confirms the scope.
- **AZ** — per-AZ host count, vCPU and RAM used/total and RAM free (summed from
  Placement), server count, the per-host table, plus the Cinder and Neutron AZ lists.
- **Flavor** — vCPU/RAM/root-disk and how many existing servers use each flavor.
- **Compute host** (after the flavor; admin) — every compute host in the chosen AZ
  with free vCPU/RAM from **Placement**, the RAM left after placing this flavor, and
  whether it fits. Enter = let the scheduler decide (the default); otherwise pick a
  host that fits (listed most-free-RAM first). The pin is sent as Nova's 2.74 `host`
  field, which still runs the scheduler and the Placement claim, so a full host is
  refused rather than overcommitted. There is no round-robin: the scheduler's RAM
  weigher already prefers the emptiest host.
- **Image** — size, min-disk/min-ram, visibility, signed?, and how many servers
  were booted from each (boot-from-volume servers aren't counted).
- **Data disks** (after security; optional) — this project's `available`, unattached
  volumes with a short ID (last 4 characters, longer if that would be ambiguous),
  name, size, bootable flag, AZ and created date. Pick by the `#`
  (e.g. `0,2`; Enter = none). A bootable / image volume (likely an old root disk,
  whose `cloudimg-rootfs`/`UEFI`/`BOOT` labels can hijack the new VM's boot) needs an
  explicit yes each, default No, and `attachstorage` will wipe it. **Nothing is attached by `createvm`**: the disks are
  saved in the spec, and once the VM is ACTIVE (or in `--dry-run`) each is re-read
  and the disks still attachable get one `cld attachstorage --serverid <vm> --disk <uuid> --disk ...`
  command; a disk that is no longer available, or belongs to another project, is left out.
- **Data volume** (`attachstorage`) — true SDS capacity from Cinder pool stats
  (total / free / allocated / over-subscription) **and** the project's volume quota
  vs usage. The requested size is validated against both.
- **Network/subnet** (`createvm`) — every usable network and its subnets with CIDR,
  gateway, and a port-count, flagging external (publicly exposed) networks. The next
  free static internal IP (lowest unused IPv4 in the allocation pool, skipping
  gateway and in-use addresses) is auto-assigned and reserved via a dedicated
  Neutron port; IPv6 / exhausted subnets fall back to Neutron auto-assignment.

> Optional Ceph cross-check (needs Ceph admin on a host, not the OpenStack API):
> `ceph df` shows raw pool usage behind the Cinder figures.

## Security behaviour (built in)

- Prompts for one or more **SSH keypairs** (comma-separated multi-select, e.g. `0,2`); warns
  if you skip them. Nova injects only one keypair at boot, so the first selected
  becomes the VM's keypair and any others are added via cloud-init `user_data`
  (requires the image to support cloud-init).
- Lists **security groups** and loudly flags any with world-open (`0.0.0.0/0`)
  ingress; requires explicit confirmation to add such a group.
  If you pick none, cld reads the project's own `default` group and reports its
  actual rules; a world-open `default` needs the same explicit yes (default No
  returns to the group menu).
- **Floating IP** assignment is off by default and needs a second confirmation;
  choosing an external network warns and re-confirms.
- Offers an **encrypted (LUKS) volume type** for the data volume when one exists.
- Warns on **community** images.
- Flags **flavors whose NUMA/hugepage extra specs cannot be satisfied** (e.g.
  `hw:numa_nodes=3` on a 4 vCPU / 8192 MB flavor, which Nova rejects with a 400);
  the flavor table marks them `!` and selecting one requires explicit
  confirmation, so the failure surfaces before any port or server is created.
- All inventory reads are **read-only**; the only writes are the explicit
  create/attach/delete actions, each gated by a final confirmation and recorded to `logs/`.
- **`deletevolume` refuses attached, transitional, cross-project and snapshotted
  volumes**. Otherwise the delete needs a `y` (default No) plus the typed ID prefix,
  and the volume is re-checked right before the call.
- **Rollback defaults to keeping resources.** On a failure mid-create, the rollback
  prompt defaults to *No* — pressing Enter leaves everything in place for
  inspection. `attachstorage` rollback only ever deletes the dangling volume, never
  the server.
- **After a failed attach, the volume's state is re-read, not assumed.** A 500 from
  Nova can land either before or after Cinder reserved the volume, so `cld` does one
  read-only re-read and reports the verified status — `available` (safe to re-run),
  `reserved`/`attaching` (a human must clear the attachment first), `in-use` (the
  attach may have succeeded), or UNKNOWN if the re-read failed. A volume Nova may
  have reserved or attached is never offered for deletion. No automatic retry is
  performed.

## Audit log

Every run appends to `logs/cld-<YYYYMMDD>.log`: the command and scope, plus each
mutation (server/volume create, port reserve, attach, volume delete, floating IP) and every
rollback delete, with resource IDs. Inventory reads are not logged.

## Notes / limitations

- AZ / host capacity needs admin (Placement + compute service reads); without it
  the tables are skipped and the host step falls back to the scheduler. It reads
  Placement because Nova's hypervisor stats are `None` at this microversion.
- Pinning a host needs the admin role (Nova policy on `host`). The `internal` AZ
  (controllers) is never offered.
- The encrypted-type detection and SDS pool stats require Cinder admin.
- This tool was previously named `osvm`; it is now `cld` (entry point `cld.py`
  or `python3 -m cld`). There are no old-name shims.
