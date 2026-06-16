# Backup & recovery of VMs and volumes

This is the operational runbook for backing up and recovering the VMs and Cinder
(Ceph/RBD) volumes on our cluster. `cld` itself does **not** perform backups — every
command here is run by a human (or a reviewed cron script) using the `openstack` CLI,
with `cld list` used as the read-only way to choose targets against live inventory.

Two facts shape everything below — read them before you rely on any procedure:

> **A Cinder *snapshot* is not a backup.** A snapshot is a fast, copy-on-write
> point-in-time that lives in the **same Ceph pool** as its source volume. It is
> perfect for "let me roll back if this change goes wrong," and useless if the pool
> itself is lost. Only a Cinder *backup* (copied off-pool) is disaster-recovery — and
> only if the `cinder-backup` service is deployed (see §0).

> **Online backups are crash-consistent, not application-consistent.** Neither `cld`
> nor the OpenStack API has in-guest access (no SSH keys, no hypervisor/libvirt — the
> same reason `attachstorage` can't auto-mount). A snapshot/backup of a *running*
> volume is equivalent to pulling the power cord: filesystems replay their journal on
> restore, but an in-flight database write may be torn. For application consistency,
> `openstack server stop <vm>` for a brief cold window (or quiesce inside the guest)
> before snapshotting.

---

## 0. Pre-flight: what's available on this cluster

Backups depend on the `cinder-backup` service. **Confirm it before you trust a backup
plan:**

```bash
# In a shell with admin creds (clears OS_* then exports its own):
source <(sudo cat /root/admin-openrc.sh)

openstack volume service list      # look for a cinder-backup host that is up/enabled
```

| `cinder-backup` state | what you have |
|-----------------------|---------------|
| up / enabled | snapshots **and** backups — full runbook applies |
| absent / down | **snapshots and server images only** — no true DR yet |

If it is absent, real off-pool backup is not possible until it is deployed (a
Kolla-Ansible / cluster task — raise it on `re-cloud-spc/rc3` with the `automation`
label). Do not let snapshots masquerade as a backup plan in the meantime.

Before any large or scheduled run, check there is room for it:

```bash
python3 cld.py list capacity --cloud <name>   # Cinder pool free + project quota
```

---

## 1. The four mechanisms (pick by intent)

| Layer | Command | Scope | DR-safe? | Use it for |
|-------|---------|-------|----------|------------|
| **Cinder snapshot** | `openstack volume snapshot create --volume <vol> [--force] <name>` | one volume, in-pool COW | **No** (same pool) | a quick rollback point before a risky change |
| **Cinder backup** | `openstack volume backup create [--incremental] [--force] --name <n> <vol>` | one volume, off-pool | **Yes** | real backups, retention, DR |
| **Server snapshot** | `openstack server image create --name <n> <server>` | boot/root disk only | via Glance store | capturing the OS/root as a redeploy template |
| **Full-VM set** | orchestrate the above across the root **and** every attached data volume | the whole VM | depends on layer | recovering a VM as one coherent unit |

Notes that bite you if you skip them:

- `--force` is **required** to snapshot or back up an **in-use (attached)** volume,
  and the result is crash-consistent (see the callout above). Without `--force` the
  volume must be `available` (detached).
- For an instance **booted from a volume**, `openstack server image create` produces
  **volume snapshots** behind a Glance image record, not a flat image — recovery then
  goes through those volume snapshots, not a plain `--image` boot.
- A server snapshot captures **only the root/boot disk**. Attached data volumes are
  *not* in it — that's what the full-VM recipe (§2) is for.

---

## 2. Full-VM-consistent backup (recipe)

Because VMs and data volumes are decoupled on this cloud, "back up a VM" means "back
up a *set*": the root disk + every attached data volume, captured together, plus
enough metadata to rebuild the server (recovery **rebuilds**, it does not "undelete").

```bash
source <(sudo cat /root/admin-openrc.sh)
VM=<server-id-or-name>
STAMP=$(date -u +%Y%m%dT%H%M)

# 1. Enumerate disks + record the volume->device map and boot source:
python3 cld.py list volumes --cloud <name>      # human view of attachments
openstack server show "$VM" -f yaml > "${VM}-${STAMP}.manifest.yaml"   # 4. manifest

# 2. (App consistency) brief cold window — skip for crash-consistent:
openstack server stop "$VM"     # wait for SHUTOFF before continuing

# 3. Back up (or snapshot) each volume with a shared, sortable name:
for VOL in <root-vol-id> <data-vol-id> ...; do
  openstack volume backup create --incremental --force \
    --name "${VM}-${VOL}-${STAMP}" "$VOL"
done

# 5. Bring it back online:
openstack server start "$VM"
```

The **manifest** (`openstack server show -f yaml`) records flavor, networks / fixed
IPs / ports, security groups, keypair, AZ, and the volume→device order — everything
§3 needs to recreate the server. Keep it next to the backups (a versioned git repo or
an object-store bucket; **not** only on the cluster you're protecting).

> Use one `$STAMP` per VM run and the same `<vm>-<vol>-<stamp>` convention everywhere
> — it's what makes a set identifiable and the retention prune in §5 possible.

---

## 3. Recovery procedures

Always run the **read-only verification** first: `python3 cld.py list volumes`,
`openstack volume show <vol>`, `openstack volume backup list` — and only act on
resources whose `status` is `available`.

**a) Roll a volume back in place (from a snapshot)** — a new volume is created from
the snapshot; the original is untouched until you swap it:

```bash
openstack volume create --snapshot <snap-id> --size <gb> <vm>-restored
# data volume: detach the old one, then:
python3 cld.py attachstorage --cloud <name> --serverid <vm> --disk <new-vol-id>
```

**b) Restore from a backup** — into a fresh volume (safest) or over an existing one:

```bash
openstack volume backup restore <backup-id> <target-volume-id>   # or --name <new>
```

**c) Recover a deleted / broken VM (full set)** — rebuild from the manifest + backups:

```bash
# 1. recreate each volume from its backup (or snapshot):
openstack volume backup restore <root-backup-id> --name <vm>-root
openstack volume backup restore <data-backup-id> --name <vm>-data
# 2. recreate the server per the saved manifest (flavor / network / SG / keypair / AZ).
#    Boot from the restored root volume:
openstack server create --volume <vm>-root --flavor <f> --network <net> \
  --security-group <sg> --key-name <kp> --availability-zone <az> <vm>
# 3. reattach data volumes in the recorded device order:
python3 cld.py attachstorage --cloud <name> --serverid <vm> --disk <data-vol-id>
# 4. reassign the floating IP if the VM had one.
```

**d) Redeploy from a server snapshot (root only)** — then bring data back with (c)/§5:

```bash
openstack server create --image <snapshot-image> --flavor <f> --network <net> <vm>
python3 cld.py attachstorage --cloud <name> --serverid <vm> --disk <restored-data-vol>
```

---

## 4. On-demand (interactive) operation

The everyday path — choose against live inventory, then run the matching command:

```bash
python3 cld.py list servers  --cloud <name>     # pick the VM
python3 cld.py list volumes  --cloud <name>     # see its volumes + attachments
python3 cld.py list capacity --cloud <name>     # confirm there's room
# ...then a snapshot/backup/server-image command from §1-§2.
```

Rules of thumb:

- Take a **snapshot before any risky change** — before a manual edit, before a
  `cld attachstorage`, before an in-guest upgrade. It's cheap and it's your undo.
- Take a **backup before decommissioning** anything, and verify it `available` before
  you delete the source.

---

## 5. Scheduled / automated operation + retention

Automate with a wrapper script on `rc3-x-3` driven by **cron or a systemd timer**.
Keep it boring, explicit, and logged — mirror `cld`'s ethos (never delete the last
good copy; log every mutation).

Design points:

- **Credentials:** use a **dedicated, least-privilege backup application credential**
  (`python3 cld.py init --project <p>` per project), not `admin-openrc.sh`.
- **Scope:** back up volumes that **opt in** via a marker — a metadata key or tag,
  e.g. `backup=daily` — never a blind "all volumes". Discover them, don't hardcode.
- **Incrementals:** a periodic full plus daily `--incremental` keeps it cheap.
- **Naming:** `<vm>-<vol>-<YYYYMMDDTHHMM>` (UTC) so backups sort and prune by age.
- **Retention (GFS):** e.g. keep **7 daily / 4 weekly / 6 monthly**; prune the rest.
- **Alert:** fail loudly — non-zero exit on any error, and watch for backups that
  land in `error` state (`openstack volume backup list`).

Prune sketch (the dangerous part — read the warning):

```bash
# Keep the newest N backups per volume; delete older ones.
for VOL in $(openstack volume list --metadata backup=daily -f value -c ID); do
  openstack volume backup list --volume "$VOL" --sort created_at:desc \
    -f value -c ID | tail -n +8 | while read -r OLD; do
      openstack volume backup delete "$OLD"      # logged by the wrapper
  done
done
```

> **Never delete a full backup that live incrementals still depend on** — removing a
> base invalidates its whole chain. Prune incrementals newest-last, and only retire a
> full once a newer full exists. When in doubt, keep it.

---

## 6. Verification / restore drills (the part everyone skips)

A backup you have never restored is a hope, not a backup.

- **Quarterly restore drill:** restore a backup to a *scratch* volume, boot a
  throwaway VM from a server snapshot, confirm the data is intact, then tear it all
  down. Record that you did it.
- **Routine health:** `openstack volume service list` (cinder-backup up),
  `openstack volume backup list` (any `error`?), and capacity trend via
  `python3 cld.py list capacity`.

---

## 7. Limits & honest caveats

- **Crash- vs application-consistency** — no guest agent; see the callout at the top.
- **Snapshots are not DR.** Even a `cinder-backup` target in a *second Ceph pool on
  the same physical cluster* is better than a snapshot but still **not off-site**. For
  true DR, the cluster needs an off-host backup target or **RBD mirroring to a
  separate Ceph cluster** — out of scope for the `openstack` CLI and for `cld`; raise
  it as a cluster-architecture item on `re-cloud-spc/rc3`.
- **`cld` does not automate any of this.** Every command here is operator-run or
  driven by a cron script you reviewed.

---

## 8. Appendix — possible future `cld` subcommands (roadmap, not built)

If we later fold this into `cld`, it must keep the tool's safety rails: every write
behind `confirm(..., default=False)`, a `--dry-run` plan path, an `audit.audit(...)`
call per mutation, `load_envvars=False` on connect, and a rollback/prune that **never
deletes the last good copy**. Likely shape, reusing existing helpers
(`render_*`, `safe_list`, `show_capacity`):

- `cld backup` — snapshot/back up a VM's whole volume set + write a manifest.
- `cld restore` — rebuild a volume or a VM from a snapshot/backup + manifest.
- `cld list backups` / `cld list snapshots` — read-only inventory.

This is a roadmap note only; nothing here is implemented.

---

See also: [docs/USAGE.md](USAGE.md) (creating VMs and attaching storage) and the
top-level [README](../README.md).
