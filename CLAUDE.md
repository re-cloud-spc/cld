# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⚠️ MAXIMUM DEFENSIVE POSTURE ON WRITE ACTIONS — READ FIRST

This is operational tooling pointed at a **live production OpenStack cluster** (the
Kolla-Ansible controller `rc3-x-3`, internal VIP `192.168.1.100`). The `cld` CLI can
create, attach, and **delete** real cluster resources — servers, Cinder volumes,
Neutron ports, application credentials, and IAM role grants. There is no staging
cloud and this directory is **not a git repo**, so there is no undo.

Treat every write as irreversible and high-blast-radius. Specifically:

- **Never run a write subcommand on your own initiative.** `cld createvm` (real
  create), `cld attachstorage` (create+attach a volume), and `cld init` (mint a
  credential / grant a role) are human-initiated only, and only when explicitly
  asked. To demonstrate or test, use the **read-only** paths: `python3 cld.py check
  --cloud <name>` and `--dry-run` on `createvm`/`attachstorage` (walks every step,
  prints the payload/plan, creates nothing).
- **Preserve the existing safety rails when editing code.** The tool's value is its
  defensiveness; do not weaken it. In particular keep: every destructive/exposing
  action gated behind an explicit `confirm(..., default=False)`; world-open
  (`0.0.0.0/0`) security-group and external-network/floating-IP double-confirmations;
  `load_envvars=False` on every `openstack.connect(...)`; `clouds.yaml` written at
  mode `600` with the secret never printed; "all inventory reads are read-only, the
  only writes are the explicit create/attach"; and an `audit.audit(...)` call on
  every mutation (see Audit logging below).
- **`confirm()` defaults must stay `False` on every delete/rollback path.** Pressing
  Enter must never destroy anything. This is the resolution of rc3 issue #112 (the
  old `_offer_rollback` defaulted to `True` and could delete a healthy ACTIVE
  server). Do not reintroduce a `default=True` on any delete path.
- **Keep storage decoupled from VM creation.** `createvm` must never create/attach
  volumes; that lives only in `attachstorage`, which operates on a pre-existing
  server and whose rollback (`_offer_volume_rollback`) deletes **only the volume,
  never the server**. This separation is the structural guarantee behind #112 — do
  not merge the two flows back together.
- When changing any code that calls `create_*`, `delete_*`, `add_auto_ip`,
  `assign_*_role`, or `create_application_credential`, re-read the surrounding
  confirm/rollback flow first, keep its `audit` call, and state in your summary
  exactly which live operations the change can trigger.

## What this is

An interactive management tool for Re:Cloud and NanoCloud admins to operate a
Ceph/RBD-backed OpenStack cloud, written to show **live cluster inventory at every
step** so choices are made against real capacity. No build system and no test
suite — a Python 3 package run directly. Subcommands:

- `cld init` — one-time-per-project: mint an application credential, write its
  `clouds.yaml` entry.
- `cld createvm` — provision a VM (cloud → AZ → flavor → image → network/subnet →
  security → confirm → create → optional floating IP). **No data-volume step.**
- `cld attachstorage` — create + attach a Cinder data volume to an **existing**
  server (capacity/quota → size/type → confirm → create+attach).
- `cld list [resource]` — read-only inventory (servers/flavors/images/networks/
  azs/capacity/clouds); default `servers`, current project unless `--all-projects`.
- `cld check` — authenticate, print the scoped project/user, exit (env-immune).

Entry points: `python3 cld.py <sub>` or `python3 -m cld <sub>`; a bare invocation or
leading flag defaults to `createvm`. (The tool was previously named `osvm`; the
rename is clean — there are no old-name shims.)

`README.md` / `docs/USAGE.md` — end-user docs; keep them in sync with behaviour
changes (they duplicate the flag tables and security-behaviour list).

## Package layout (`cld/`)

```
cli.py        argparse subcommands + dispatch; createvm orchestration (run_interactive)
cloud.py      clouds.yaml discovery, connect (load_envvars=False), safe_list, select_cloud, run_check
inventory.py  Inventory: one cross-project server snapshot -> usage counts
steps.py      createvm wizard steps: project/AZ/flavor/image/network(+next_available_ip)/security.
              Each selector is split render_* (table only) + select_* (render then prompt);
              `list` reuses the render_* halves.
listcmd.py    read-only `cld list` orchestrator (reuses steps.render_*, volume.show_capacity)
volume.py     SDS capacity + quota display; show_capacity() / prompt_volume_spec()
vm.py         build_payload, _reserve_port, create_vm (NO volume), _offer_rollback (default=False),
              print_summary; key_names()/_keypair_user_data() split SSH keys across Nova + cloud-init
storage.py    attach_storage() + _offer_volume_rollback (volume-only, never the server)
init.py       credential bootstrap (connect_admin, ensure_admin_role, merge_clouds_entry)
answers.py    save/load a createvm spec (YAML)
audit.py      runtime logging wiring -> logs/cld-<date>.log; audit()/warn()
ui.py         out/header/warn/err/render_table + choose/prompt_*/confirm/gb (rich-optional)
```

`steps.py` is the natural seam to split into a `steps/` subpackage as steps grow.

## Commands

```bash
pip install -r requirements.txt           # openstacksdk, rich (optional), PyYAML

# Read-only / safe:
python3 cld.py check --cloud <name>                 # auth smoke test, prints scope, exits
python3 cld.py list [resource] [--cloud <name>] [--all-projects]   # inventory, no writes, unlogged
python3 cld.py createvm --cloud <name> --dry-run    # full wizard, prints payload, creates nothing
python3 cld.py attachstorage --cloud <name> --server <s> --dry-run   # plan only

# Writes to the cluster — human-initiated only:
python3 cld.py createvm --cloud <name> [--save-answers f.yaml]
python3 cld.py createvm --non-interactive f.yaml
python3 cld.py attachstorage --cloud <name> --server <s> [--size GB] [--type T]
python3 cld.py init --project <p> [--cloud <c>] [--admin-role]
```

There is no linter or test runner. Validate changes with `python3 -c "import
cld.cli"` (import smoke), `cld check`, and `--dry-run` against a real cloud entry;
confirm a `logs/cld-*.log` line appears for each invocation.

## Architecture notes that span files

- **App-credential-per-project auth model.** Application credentials are permanently
  bound to one project and cannot be re-scoped. This is the central design constraint:
  each project becomes its own named `clouds.yaml` "cloud", the first step is "pick the
  cloud (= project)", and `steps.current_project(conn)` reads the fixed scope rather
  than letting you choose. `cld init` (`init.py`) creates these entries.

- **`load_envvars=False` is load-bearing, not incidental.** Operators source
  `/root/admin-openrc.sh` (which exports `OS_*`) in the same shell. Those vars would
  inject a project scope that app-cred auth rejects with *"Application credentials
  cannot request a scope"* (HTTP 401 from the raw `openstack` CLI). `cloud.connect()`
  sidesteps this everywhere by passing `load_envvars=False`; `cld check` exists
  specifically as an env-immune way to verify auth.

- **#112 is fixed structurally, not by retry.** VM creation and volume attach are two
  separate subcommands. `attachstorage` runs against a pre-existing server, so a
  transient Cinder failure (rc3 #111) can only roll back the dangling volume, never
  the VM; `createvm` has no fragile post-ACTIVE step. All rollback `confirm()`s
  default to `False`. (There is intentionally **no** auto-retry of Cinder calls.)

- **Inventory is a single cross-project snapshot.** `Inventory` fetches all servers
  once (`all_projects=True`) and each step annotates its menu with usage counts.
  Admin-only reads (hypervisor stats, Cinder pool capacity, aggregates) degrade
  gracefully via `cloud.safe_list()`, which catches everything and returns `[]` — so
  missing the admin role yields empty tables, not crashes.

- **Static-IP reservation is a two-phase dance with a race retry.**
  `steps.next_available_ip()` computes the lowest free IPv4 in the subnet's allocation
  pools (display time); `vm._reserve_port()` then creates a dedicated Neutron port for
  it at create time, retrying up to 5× on allocation races before falling back to
  Neutron auto-assignment. The reserved port is created *before* the server and is
  cleaned up by rollback. (This per-IP race retry is unrelated to Cinder; do not
  generalize it into a Cinder retry.)

- **Audit logging.** `audit.get_logger()` lazily wires one `FileHandler` to
  `logs/cld-<YYYYMMDD>.log` (idempotent `makedirs`; this is runtime wiring, not a
  setup step). Every mutation and rollback delete calls `audit.audit(action, id=...)`
  / `audit.warn(...)` with resource IDs; reads are not logged. Keep adding an audit
  call to any new write.

- **SSH keys: a list split across two delivery mechanisms.** The security step
  multi-selects keypairs (`security["key_names"]`, a list; `steps.security_review`
  via `ui.choose_multi`). Nova injects **only one** keypair at boot, so
  `vm.build_payload` sends `key_names[0]` as `key_name` and the remainder as
  cloud-init `user_data` (a base64 `#cloud-config ssh_authorized_keys` block built
  by `vm._keypair_user_data`, which reads each `conn.compute.get_keypair().public_key`).
  1 key → `key_name` only (no `user_data`); 0 → neither. `vm.key_names()` tolerates a
  legacy single `key_name` (str) in old saved specs. Do **not** collapse this back to
  a single `key_name` — multi-key needs the cloud-init path.

- **Optional `rich`.** `ui.out()` / `ui.render_table()` degrade to plain text
  (stripping inline markup) when `rich` isn't importable, so keep using the
  `[tag]...[/tag]` markup convention and let the helpers handle absence.

- **Resilience idiom.** List calls go through `safe_list()`; mutating calls use
  explicit `try/except os_exc.SDKException` (or broad `except`) with user-facing
  `err()`/`warn()`, an `audit` call, and — where a partial resource may exist — a
  rollback offer. Match this idiom: never let a mutating failure pass silently, always
  account for the half-created resource, and never default its cleanup prompt to Yes.

## Project context

Issues for this tool and its cluster go to the private GitHub repo `re-cloud-spc/rc3`
(`gh` is authed as `euroblaze`; useful labels: `bug`, `automation`). Issue #112 (the
destructive rollback default) is addressed here by decoupling storage + defaulting
rollback to No. #111 (transient `CinderConnectionFailed` on attach — HAProxy
keep-alive race) is a cluster-side issue; `cld` deliberately does **not** paper over
it with client retries — a failed attach leaves the server untouched and the volume
recoverable.
