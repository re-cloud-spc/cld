# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⚠️ MAXIMUM DEFENSIVE POSTURE ON WRITE ACTIONS — READ FIRST

This is operational tooling pointed at a **live production OpenStack cluster** (the
Kolla-Ansible controller `rc3-x-3`, internal VIP `192.168.1.100`). The `cld` CLI can
create, attach, and **delete** real cluster resources — servers, Cinder volumes,
Neutron ports, application credentials, and IAM role grants. There is no staging
cloud, so there is no undo on the cluster side. (The *code* is a git repo --
`re-cloud-spc/cld` on GitHub, worked via branch + PR onto `main` -- but a rolled-back
commit does not un-delete a server.)

Treat every write as irreversible and high-blast-radius. Specifically:

- **Never run a write subcommand on your own initiative.** `cld createvm` (real
  create), `cld attachstorage` (create+attach a volume), `cld deletevolume`
  (permanently delete a volume), and `cld init` (mint a credential / grant a role)
  are human-initiated only, and only when explicitly
  asked. To demonstrate or test, use the **read-only** paths: `python3 cld.py check
  --cloud <name>` and `--dry-run` on `createvm`/`attachstorage`/`deletevolume` (walks
  every step, prints the payload/plan/report, changes nothing).
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
- `cld createvm` — provision a VM (cloud → AZ → flavor → optional host pin → image → network/subnet →
  security → data disks (plan only) → confirm → create → optional floating IP).
  **Never attaches storage**: the disk step only selects existing volumes and prints
  `attachstorage --disk` commands after ACTIVE.
- `cld attachstorage` — create + attach a Cinder data volume to an **existing**
  server (capacity/quota → size/type → confirm → create+attach).
- `cld list [resource]` — read-only inventory (servers/volumes/flavors/images/
  networks/azs/hosts/capacity/clouds); default `servers`, current project unless
  `--all-projects`. `volumes` shows each Cinder volume + the VM it's attached to +
  created/updated (UTC). Cinder has no last-accessed/I/O timestamp, so `updated_at`
  (last record change) is the recency stand-in — don't try to add "last accessed".
  `--available [0|1]` filters `volumes` by Cinder status: `1` (or bare `--available`,
  `nargs="?"`, `const=1`) = `status == "available"`,
  `0` = its exact complement (so 1/0 partition the list; `error`/`creating` volumes
  land in `0`, which is intentional — they're the ones worth surfacing). It's the only
  non-`store_true` switch in the CLI because it needs three states (1 / 0 / omitted);
  passing it with any other resource is a hard error, not a silent no-op.
- `cld deletevolume --volumeid <uuid>` — permanently delete ONE unattached volume,
  after a report of its Cinder facts, on-disk contents (read-only Ceph peek), age and
  cld audit history. Refuses attached/transitional/cross-project/snapshotted volumes
  and tells the operator to detach first; it never detaches.
- `cld check` — authenticate, print the scoped project/user, exit (env-immune).
- `cld completion [bash]` — print a bash completion script generated from
  `build_parser()` (local only, no connection, unlogged). Adding a flag or subcommand
  needs no completion edit; only new value types (e.g. another file flag →
  `completion._FILE_DESTS`) do.

Entry points: `python3 cld.py <sub>` or `python3 -m cld <sub>`; a bare invocation or
leading flag defaults to `createvm`. (The tool was previously named `osvm`; the
rename is clean — there are no old-name shims.)

`README.md` / `docs/USAGE.md` — end-user docs; keep them in sync with behaviour
changes (they duplicate the flag tables and security-behaviour list).
`docs/BACKUP_RECOVERY.md`, `docs/SWAP_SETUP.md` — human runbooks for procedures `cld`
deliberately does **not** automate (Cinder snapshot/backup; in-guest swap). They are run
by hand with the `openstack` CLI + in-guest shell; do not turn them into subcommands
without the access they assume.

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
storage.py    attach_storage(): create+attach a new volume, OR with --disk attach an
              EXISTING volume (_attach_existing: only if available/unattached/same-project,
              and NEVER modifies/deletes the pre-existing volume); _offer_volume_rollback
              (deletes only volumes cld created, never the server). After a successful
              attach, _print_mount_help prints the in-guest device as its exact
              /dev/disk/by-id/virtio-<vol-id[:20]> path (NOT Nova's /dev/vdX, which the
              guest may name differently) + mount steps, and for an image/root volume
              warns that its cloudimg-rootfs/UEFI/BOOT labels can hijack the VM's next
              boot (_warn_boot_label_clash). cld
              does NOT mount (no SSH keys, no hypervisor/libvirt; the API can't mount a
              guest FS), so don't add an auto-mount path without that access.
              A failed attach re-reads the volume from Cinder (_recheck_volume) and
              reports its VERIFIED status; it never asserts "unchanged".
deletevol.py  delete_volume(): the only delete of a pre-existing resource. Hard refusals
              (attached incl. Nova record + RBD watcher, non-final status, other project,
              snapshots -- never cascade), a report, confirm_destructive + typed ID prefix,
              re-read before the call, force=False, audit, verify gone. Never detaches.
rbdpeek.py    READ-ONLY look at a volume's Ceph image (librbd read_only, atime updates
              disabled for this client): allocated bytes, partitions, fs superblocks.
              Self-contained: re-runs itself via `sudo -n` when the keyring is root-only.
hosts.py      per-compute-host capacity from Placement (host_capacity/host_fit/render_hosts)
              + the optional createvm host picker (select_host). Read-only.
completion.py `cld completion bash`: completion script generated from the argparse parser;
              --cloud names parsed by awk at TAB time (names only, never values).
disks.py      createvm "data disks" step: select available project volumes (# as every menu,
              short IDs, bootable needs a yes), re-read them (recheck_disks) and print
              full-UUID `attachstorage --disk` lines. Read-only; never attaches.
init.py       credential bootstrap (connect_admin, ensure_admin_role, merge_clouds_entry)
answers.py    save/load a createvm spec (YAML)
audit.py      runtime logging wiring -> logs/cld-<date>.log; audit()/warn()
ui.py         out/header/warn/err/render_table + choose/prompt_*/confirm/gb (rich-optional)
              plus Abort + confirm_destructive (the universal-escape contract, below)
```

`steps.py` is the natural seam to split into a `steps/` subpackage as steps grow.

## Commands

```bash
pip install -r requirements.txt           # openstacksdk, rich (optional), PyYAML

# Read-only / safe:
python3 cld.py check --cloud <name>                 # auth smoke test, prints scope, exits
python3 cld.py list [resource] [--cloud <name>] [--all-projects] [--available [0|1]]  # inventory, no writes, unlogged
python3 cld.py createvm --cloud <name> --dry-run    # full wizard, prints payload, creates nothing
python3 cld.py attachstorage --cloud <name> --serverid <id> --dry-run  # plan only
python3 cld.py deletevolume --cloud <name> --volumeid <uuid> --dry-run  # report only

# Writes to the cluster — human-initiated only:
python3 cld.py createvm --cloud <name> [--host <compute-host>] [--save-answers f.yaml]
python3 cld.py createvm --non-interactive f.yaml
python3 cld.py attachstorage --cloud <name> --serverid <id> [--size GB] [--type T]
python3 cld.py deletevolume --cloud <name> --volumeid <uuid>
python3 cld.py init --project <p> [--cloud <c>] [--admin-role]
```

There is no linter or test runner. Validate changes with `python3 -c "import
cld.cli"` (import smoke), `cld check`, and `--dry-run` against a real cloud entry;
confirm a `logs/cld-*.log` line appears for each invocation.

## Architecture notes that span files

- **Every wizard step must offer only options valid in the fixed project scope.**
  The credential's project is chosen once (cloud = project) and cannot change, so a
  step that lists another project's resources can only mislead. With the admin role,
  `conn.network.*` reads return *all* projects — filter on `project_id` at the step.
  `steps.render_networks` is the reference implementation, including the one
  deliberate exception: a foreign network that is `shared` or `router_external` IS
  usable, so it's kept. Security groups have **no** such exception (there is no
  shared SG) and are filtered strictly to `(project_id, None)` in `security_review`
  — required, not cosmetic, because `vm.build_payload` sends SG *names* and Nova
  re-resolves them in the target project, so an unfiltered menu can warn about
  world-open rules belonging to a different group than the one actually attached.
  When none is picked, `steps._accept_default_sg` reads the project's real `default`
  group (rc3's admin one is world-open) instead of assuming stock rules, and a
  world-open default needs the same `confirm(default=False)` as picking one by hand.
  Flavors/images/keypairs/AZs are not project-partitioned here and need no filter.

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

- **A failed attach reports verified state, never assumed state.** A Nova 500 on
  `POST /os-volume_attachments` can land on either side of Cinder reserving the volume,
  so the pre-attach snapshot proves nothing. `storage._recheck_volume()` does one
  read-only `get_volume` in the failure handler and reports what is actually there:
  `available` (safe to re-run), `reserved`/`attaching` (Nova got in first — a re-run is
  refused, a human must clear the attachment), `in-use` (the attach may have *succeeded*),
  `error`, or UNKNOWN when the re-read itself fails. That status is also written to the
  audit line as `status_after=`, which is what makes a post-mortem answerable from
  `logs/cld-*.log` alone. It gates `_offer_volume_rollback` too: a volume Nova may have
  reserved or attached is never offered for deletion at all (unverified still prompts,
  still defaulting to keep). This is reporting only — **no retry** was added; see #111.

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

- **Every prompt is escapable, and escaping never destroys (rc3 #116).** All
  numeric/menu prompts funnel through `ui._ask()`, which raises `ui.Abort` on Ctrl-D or
  `q`/`quit`/`cancel`; free text uses `ui._ask_text()`, where Ctrl-D aborts but `q` is a
  legitimate value (a VM may be named `q`). `Abort` is caught **once**, in `cli.main()`
  ("Aborted; nothing created", exit 130), so no caller needs a "did they cancel?" branch
  and a half-built spec cannot leak past a cancelled prompt. The one place `Abort` must
  not propagate is a rollback prompt: `ui.confirm_destructive()` maps it to `False`, so
  quitting at "Delete the volume that failed to attach?" keeps the resource. Use
  `confirm_destructive` — not `confirm` — for every delete/rollback question, and add new
  prompts via the `ui` helpers rather than bare `input()`.
  Every menu is **zero-based** (`0` = first item; multi-select `0,2`, kept in
  typed order). A none/skip option has no number -- it is **Enter only** -- so no
  digit means "skip" in one menu and "pick" in another.

- **Flavors are preflighted for unsatisfiable NUMA/hugepage extra specs.**
  `steps._flavor_topology()` returns `(note, blocker)`: `hw:numa_nodes=N` that does not
  divide the flavor's vCPU/RAM evenly (without explicit `hw:numa_cpus.N`/`hw:numa_mem.N`)
  is a hard **blocker** — Nova rejects the boot with a 400 — and gets a red `!` in the
  table plus a warning. `hw:mem_page_size` is only a **soft note**: whether hugepages are
  reserved is a compute-host fact `cld` cannot read, so it warns about "No valid host"
  rather than blocking. The `topology` column is appended only when some flavor has
  specs, keeping the common table unchanged. Preserve that blocker/note distinction —
  promoting the soft note to a blocker hides usable flavors.

- **SSH keys: a list split across two delivery mechanisms.** The security step
  multi-selects keypairs (`security["key_names"]`, a list; `steps.security_review`
  via `ui.choose_multi`). Nova injects **only one** keypair at boot, so
  `vm.build_payload` sends `key_names[0]` as `key_name` and the remainder as
  cloud-init `user_data` (a base64 `#cloud-config ssh_authorized_keys` block built
  by `vm._keypair_user_data`, which reads each `conn.compute.get_keypair().public_key`).
  1 key → `key_name` only (no `user_data`); 0 → neither. `vm.key_names()` tolerates a
  legacy single `key_name` (str) in old saved specs. Do **not** collapse this back to
  a single `key_name` — multi-key needs the cloud-init path.

- **Host pinning goes through the scheduler, never around it.** Per-host capacity
  comes from Placement (`hosts.host_capacity`), not hypervisor stats, which are
  `None` at this Nova microversion. `select_host` (after the flavor, default =
  scheduler) offers only hosts in the chosen AZ that fit the flavor; the pin is
  sent as the Nova 2.74 `host` field via `vm.PinnedServer`, because the SDK's
  `create_server(host=...)` silently drops it. Do **not** switch to the old
  `az:host` forced-host syntax: it skips the scheduler filters. No round-robin
  placement: the RAM weigher already spreads, and these hosts differ in size.
  `createvm --host` takes the same path without the menu (`cli._resolve_host`:
  unknown/down host = exit before any question; no-fit = `confirm(default=False)`)
  and sets the AZ to the host's, so the two can never disagree.

- **createvm plans disks, attachstorage attaches them.** `disks.select_disks` keeps
  #112's structure: the selection is stored in the spec (`spec["disks"]`) and is
  **never** in the create payload; after ACTIVE (or in `--dry-run`) each disk is
  re-read and gets a printed `cld attachstorage --disk <full-uuid>` line, whose own
  checks (available, unattached, same project, bootable confirm) run at attach time.
  Short IDs are for display only -- commands and warnings always use the full UUID.
  Do not turn the printed lines into an automatic attach inside createvm without
  explicitly changing the storage-decoupling rule above.

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
