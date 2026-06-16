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
python3 cld.py list flavors                  # vCPU / RAM / disk + in-use count
python3 cld.py list images
python3 cld.py list networks
python3 cld.py list azs                       # availability zones + compute capacity
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
| `azs` | availability zones + per-AZ compute capacity (admin) |
| `capacity` | Cinder SDS pool capacity + this project's volume quota |
| `clouds` | configured `clouds.yaml` entries (each = one project); local, no auth |

| flag | effect |
|------|--------|
| `--cloud NAME` | cloud (= project) from clouds.yaml (not needed for `clouds`) |
| `--all-projects` | `servers`/`volumes`: include every project (admin); default is the current project |

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

Flow: **cloud (project) → availability zone → flavor → image → network/subnet →
security → confirm → create** (then an optional floating IP). When the VM is
ACTIVE, `cld` prints the exact `attachstorage` command to add a data volume.

## 5. Add a data volume (`cld attachstorage`)

```bash
# Interactive — lists the project's servers, then shows capacity/quota:
python3 cld.py attachstorage --cloud admin

# Target a specific server, size and type inline:
python3 cld.py attachstorage --cloud admin --serverid 3f1c8d2a-... --size 50 --type encrypted

# Attach an EXISTING volume by ID (instead of creating one):
python3 cld.py attachstorage --cloud admin --serverid 3f1c8d2a-... --disk dedf1d0a-...

# See what would happen without creating/attaching anything:
python3 cld.py attachstorage --cloud admin --serverid 3f1c8d2a-... --size 50 --dry-run
```

| flag | effect |
|------|--------|
| `--cloud NAME` | cloud (= project) the server lives in |
| `--serverid ID` | target server ID (otherwise lists the project's servers to pick) |
| `--disk VOLUME_ID` | attach an existing volume by ID instead of creating one; attaches only if it's `available` and unattached, and never alters the volume on failure |
| `--size GB` | volume size for a new volume (ignored with `--disk`) |
| `--type TYPE` | volume type, e.g. an encrypted/LUKS type (ignored with `--disk`) |
| `--dry-run` | show what would be created/attached, change nothing |

Before you commit it shows **true SDS capacity** (Cinder pool total / free /
allocated / over-subscription) and your project's volume quota vs usage, and
validates the size against both. The disk model: a VM boots onto the flavor's small
Ceph root disk; extra capacity comes from these attached Cinder (Ceph RBD) volumes.

## 6. What each createvm step shows you

- **Cloud (project)** — the project is fixed by the chosen credential entry.
- **Availability zone** (only prompts if more than one) — per-AZ host count, vCPU
  and RAM used/total, server count, plus the Cinder and Neutron AZ lists.
- **Flavor** — vCPU / RAM / root disk (the small Ceph-backed boot disk) and how
  many existing servers use each flavor.
- **Image** — size, min-disk / min-ram, visibility, whether it's signed, and how
  many servers were booted from each (boot-from-volume servers aren't counted).
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
  `1,3`); warns if you skip them. Nova injects only one keypair at boot, so the
  first selected becomes the VM's keypair and the rest are added via cloud-init
  `user_data` (requires the image to support cloud-init).
- Lists **security groups** and loudly flags any with world-open (`0.0.0.0/0`)
  ingress; adding such a group requires explicit confirmation.
- **Floating IP** assignment is off by default and needs a second confirmation;
  choosing an external network warns and re-confirms.
- Offers an **encrypted (LUKS) volume type** for the data volume when available.
- Warns on **community** images.
- All inventory reads are **read-only**; the only writes are the explicit
  create/attach actions, each gated by a final confirmation.
- **Rollback defaults to keeping resources** — on a mid-create failure the rollback
  prompt defaults to *No*, so Enter leaves everything for inspection.
  `attachstorage` rollback only ever deletes the dangling volume, never the server.
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

- The VM boots from the image onto the flavor's small Ceph/RBD root disk; extra
  capacity comes from `attachstorage` volumes, not the root disk.
- Hypervisor→AZ mapping relies on host aggregates carrying an `availability_zone`;
  hosts in no such aggregate show under `(unmapped)`.
- Optional raw-Ceph cross-check (needs Ceph admin on a host, not the OpenStack
  API): `ceph df` shows pool usage behind the Cinder figures.
