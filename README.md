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
| `cld check` | authenticate, print the scoped project/user, and exit |

> **VM creation and data-volume attachment are deliberately two separate steps.**
> `createvm` never creates volumes; afterwards you run `attachstorage` against the
> running server. Beyond being composable, this is a safety property: a transient
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
python3 cld.py list flavors                  # vCPU / RAM / disk + in-use count
python3 cld.py list capacity                 # Cinder pool capacity + volume quota
python3 cld.py list clouds                   # configured clouds.yaml entries (no auth needed)
```

| resource | shows |
|----------|-------|
| `servers` (default) | name, ID, status, flavor, IP, AZ (`+ project` with `--all-projects`) |
| `flavors` | vCPU / RAM / root disk + how many servers use each |
| `images` | visibility, size, min-disk/ram, signed, in-use count |
| `networks` | networks + subnets: CIDR, gateway, kind, port count |
| `azs` | availability zones + per-AZ compute capacity (admin) |
| `capacity` | Cinder SDS pool capacity + this project's volume quota |
| `clouds` | configured `clouds.yaml` entries (each = one project); local, no auth |

| flag | effect |
|------|--------|
| `--cloud NAME` | cloud (= project) from clouds.yaml (not needed for `clouds`) |
| `--all-projects` | `servers`: include every project (admin); default is the current project |

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
```

### `createvm` flags

| flag | effect |
|------|--------|
| `--cloud NAME` | use this named cloud (= project) from clouds.yaml (otherwise prompts) |
| `--dry-run` | walk every step, print the create payload, change nothing |
| `--save-answers FILE` | write this run's spec to a YAML file |
| `--non-interactive FILE` | replay a saved spec with no prompts |

### `attachstorage` flags

| flag | effect |
|------|--------|
| `--cloud NAME` | cloud (= project) the server lives in |
| `--serverid ID` | target server ID (otherwise lists the project's servers to pick) |
| `--size GB` | volume size (otherwise prompts) |
| `--type TYPE` | volume type, e.g. an encrypted/LUKS type (otherwise prompts) |
| `--dry-run` | show what would be created/attached, change nothing |

## What "inventory" means at each step

- **Project** — fixed by the chosen credential; the tool re-confirms the scope.
- **AZ** — per-AZ host count, vCPU and RAM used/total (from hypervisor stats),
  server count, plus the Cinder and Neutron AZ lists.
- **Flavor** — vCPU/RAM/root-disk and how many existing servers use each flavor.
- **Image** — size, min-disk/min-ram, visibility, signed?, and how many servers
  were booted from each (boot-from-volume servers aren't counted).
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

- Prompts for one or more **SSH keypairs** (comma-separated multi-select); warns
  if you skip them. Nova injects only one keypair at boot, so the first selected
  becomes the VM's keypair and any others are added via cloud-init `user_data`
  (requires the image to support cloud-init).
- Lists **security groups** and loudly flags any with world-open (`0.0.0.0/0`)
  ingress; requires explicit confirmation to add such a group.
- **Floating IP** assignment is off by default and needs a second confirmation;
  choosing an external network warns and re-confirms.
- Offers an **encrypted (LUKS) volume type** for the data volume when one exists.
- Warns on **community** images.
- All inventory reads are **read-only**; the only writes are the explicit
  create/attach actions, each gated by a final confirmation and recorded to `logs/`.
- **Rollback defaults to keeping resources.** On a failure mid-create, the rollback
  prompt defaults to *No* — pressing Enter leaves everything in place for
  inspection. `attachstorage` rollback only ever deletes the dangling volume, never
  the server.

## Audit log

Every run appends to `logs/cld-<YYYYMMDD>.log`: the command and scope, plus each
mutation (server/volume create, port reserve, attach, floating IP) and every
rollback delete, with resource IDs. Inventory reads are not logged.

## Notes / limitations

- AZ capacity needs admin (hypervisor + aggregate reads); without it that table
  is skipped gracefully.
- Hypervisor→AZ mapping uses host aggregates that carry an `availability_zone`;
  hosts in no such aggregate show under `(unmapped)`.
- The encrypted-type detection and SDS pool stats require Cinder admin.
- This tool was previously named `osvm`; it is now `cld` (entry point `cld.py`
  or `python3 -m cld`). There are no old-name shims.
