# Swap setup runbook — KJ-STAG (`ecotech-repair` / Odoo staging)

Context: KJ-STAG (`ca90c0bc-a8c0-4060-8a6b-0aaddc6abaa1`, `10.0.100.43`) was OOM-wedged on
2026-07-28 — 4 GiB RAM, **no swap**, Odoo killed by the OOM killer after ~88 days uptime.
A 10 GB Cinder volume (`kj-stag-swap`, `56e6afce-ae83-4e39-88db-b5306aa511ca`) is now
attached as `/dev/vdb` to serve as a safety net.

This runbook covers doing that safely, and specifically how to keep a bad fstab entry from
leaving the VM unbootable.

---

## 0. Two facts to get straight first

**Swap does not show up in `df -h`. Ever.** `df` lists mounted filesystems; swap is raw
block space with no filesystem and no mountpoint. Verify swap with these instead:

```bash
swapon --show      # device, type, size, used, priority
free -h            # the Swap: row
cat /proc/swaps
```

If you want capacity visible in `df -h`, you need a **mounted filesystem**, which is a
different use of the disk. See the split layout in §3 — it gives you both.

**Swap here lives on Ceph network storage, on the slower pool.** `vda` is on
`volumes_ssd`; this volume is on `volumes`. Under real memory pressure the guest will page
over the network. That converts a fast, clean OOM kill into a long period where the VM is
technically alive but unusably slow — which is arguably *harder* to detect than the crash
it replaces. Swap is a backstop that buys time to react; it is not a fix, and it does not
reduce Odoo's memory appetite. Pair it with §7.

---

## 1. Prerequisite: get a shell (currently the blocker)

There is **no usable SSH key** for this VM — `key_name` is `None` in Nova, and neither
`~/.ssh/id_ed25519` nor `~/.ssh/k0s-vm-default-key.pem` authenticates (`Permission denied
(publickey)`). The qemu-guest-agent is also not connected, so the
`rc3-reset-vm-password` skill cannot help either — it depends on that agent.

So you need one of:

- **Horizon console (VNC)** with a known in-guest password, or
- `openstack --os-cloud admin console url show ca90c0bc-a8c0-4060-8a6b-0aaddc6abaa1 novnc`
  and log in at the `kj-stag login:` prompt, or
- the Odoo developer, who presumably has working credentials.

Note the tenant network is **not reachable** from the controller or compute host directly.
To reach `10.0.100.43` over the network, proxy through the OVN namespace on `rc3-c-1`:

```bash
NS=ovnmeta-8d361425-d44a-47f8-8221-072cdec1d4b5
ssh -i ~/.ssh/<key> \
  -o ProxyCommand="ssh -q ashant@rc3-c-1 sudo ip netns exec $NS nc %h %p" \
  ubuntu@10.0.100.43
```

---

## 2. Identify the disk — the step that can destroy the VM

`mkswap` on the wrong device **irreversibly destroys the Odoo root filesystem.** Do not
trust `/dev/vdb`; kernel device names are assigned in discovery order and can change
across reboots. Both disks expose their Cinder UUID as a SCSI serial, so use the stable
`by-id` path:

| Disk | Cinder volume | Size | **Status** |
|---|---|---|---|
| `vda` | `1f08ec80-08e7-4e39-afb8-c445b8da28ad` | 40 GB | ☠️ **ROOT DISK — never touch** |
| `vdb` | `56e6afce-ae83-4e39-88db-b5306aa511ca` | 10 GB | ✅ target |

Both were confirmed via libvirt to expose their full Cinder UUID as the disk serial. Derive
the real path in-guest rather than assuming it — QEMU truncates virtio serials and the
cutoff length varies by version, so the `by-id` name is a **prefix** of the UUID:

```bash
ls -l /dev/disk/by-id/ | grep -E '56e6afce|1f08ec80'
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,SERIAL
findmnt /                      # shows which device is root — must NOT be your target
```

Take the `56e6afce…` entry from that `ls` output — that exact string is your `$DISK` in §3.

**Gate: the target must be 10G, have no mountpoint, and its serial must start `56e6afce`.**
If root resolves to the same disk you are about to wipe, stop.

The volume currently still holds an old `ubuntu-heat` image filesystem — `lsblk -f` will
show a filesystem on it. That is expected; erasing it is intended and safe (it was an idle
clone, and both its parent snapshot `e3835655` and source volume `017c7a81` still exist).

---

## 3. Choose a layout

Set a shell variable first so no command below ever hardcodes `/dev/vdb`:

```bash
DISK=/dev/disk/by-id/virtio-56e6afce-ae83-4e39-8
```

### Option A — whole disk as swap (simplest)

10 GB of swap against 4 GB of RAM is 2.5× RAM. On slow network storage that is a lot of
rope: a memory leak can fill all of it, and the VM stays catatonic for a long time before
anything kills it. Acceptable only with `earlyoom` from §7.

### Option B — split: 4 GB swap + 6 GB data (recommended)

Bounds the thrash window, and the data partition **does** appear in `df -h`, which is what
you asked for.

```bash
sudo parted -s "$DISK" mklabel gpt
sudo parted -s "$DISK" mkpart primary linux-swap 1MiB 4GiB
sudo parted -s "$DISK" mkpart primary ext4 4GiB 100%
sudo partprobe "$DISK"; sleep 2
lsblk "$DISK"                       # expect two partitions
```

Partitions appear as `${DISK}-part1` and `${DISK}-part2`.

---

## 4. Make it live — reversible, nothing persisted yet

```bash
sudo mkswap "${DISK}-part1"                    # Option A: sudo mkswap "$DISK"
sudo swapon "${DISK}-part1"                    # Option A: sudo swapon "$DISK"

swapon --show
free -h                                        # Swap: row should now be non-zero
```

Option B, the data partition:

```bash
sudo mkfs.ext4 -L kjstag-data "${DISK}-part2"
sudo mkdir -p /mnt/data
sudo mount "${DISK}-part2" /mnt/data
df -h /mnt/data                                # <-- this is what shows in df -h
```

**Nothing here survives a reboot yet.** If anything looks wrong, `sudo swapoff -a` and
`sudo umount /mnt/data` puts you exactly back. Confirm it all looks right before §5.

---

## 5. Persist it safely — where reboots actually break

Get the UUIDs (these are stable identities, unlike `/dev/vdb`):

```bash
sudo blkid "${DISK}-part1" "${DISK}-part2"
SWAP_UUID=$(sudo blkid -s UUID -o value "${DISK}-part1")
DATA_UUID=$(sudo blkid -s UUID -o value "${DISK}-part2")
echo "swap=$SWAP_UUID data=$DATA_UUID"
```

Back up fstab **before** editing — this is your undo:

```bash
sudo cp /etc/fstab /etc/fstab.bak-$(date +%F)
```

Append, using UUIDs and failure-tolerant options:

```bash
echo "UUID=$SWAP_UUID none swap sw,nofail,x-systemd.device-timeout=10s 0 0" | sudo tee -a /etc/fstab
echo "UUID=$DATA_UUID /mnt/data ext4 defaults,nofail,x-systemd.device-timeout=10s 0 2" | sudo tee -a /etc/fstab
```

Why each option matters:

- **`UUID=` not `/dev/vdb`** — if the device ever renumbers (a second volume attached, a
  rebuild, a migration), `/dev/vdb` could point at a *different* disk. At best swap fails;
  at worst you have an fstab line aimed at the wrong device.
- **`nofail`** — without it, a missing device makes systemd's `local-fs.target` fail and
  drops the boot into **emergency mode**, needing console access to fix. Since this volume
  is a separate Cinder resource that can be detached, `nofail` is not optional here.
- **`x-systemd.device-timeout=10s`** — without it systemd waits the default 90 s for a
  missing device before giving up, turning a reboot into a two-minute stall.
- **`0 0` / `0 2`** — the final `0` for swap disables fsck (meaningless for swap). The `2`
  on the data partition means fsck it after root.

---

## 6. Verify persistence WITHOUT rebooting

This is the important part — prove the config is good *before* you trust it to a reboot.

```bash
sudo systemctl daemon-reload           # regenerate units from fstab
sudo findmnt --verify --verbose        # validates fstab syntax and targets
```

Then exercise the generated units for real:

```bash
sudo swapoff -a && sudo umount /mnt/data      # tear down the manual setup
sudo systemctl daemon-reload
sudo swapon -a && sudo mount -a               # bring it back up *from fstab*
swapon --show && df -h /mnt/data              # both must be back
systemctl list-units --type=swap              # unit should be 'active'
```

If that round-trip works, the fstab entries are sound. Only then reboot:

```bash
sudo reboot
# after it returns:
swapon --show; free -h; df -h /mnt/data
systemctl --failed                            # must be empty
journalctl -b -p err                          # no device/mount errors
```

---

## 7. Tuning and the honest trade-off

```bash
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/99-swap.conf
sudo sysctl --system
```

`swappiness=10` keeps swap as a reserve rather than a routine destination — important when
paging costs a network round trip.

**Strongly recommended alongside slow swap:** `earlyoom`. It kills the largest memory
consumer *before* the box becomes catatonic, restoring the fast-failure behaviour that
swap otherwise trades away.

```bash
sudo apt-get install -y earlyoom
sudo systemctl enable --now earlyoom
```

**Alternative worth considering:** `zram` — compressed swap in RAM, no network I/O at all.
On a 4 GiB box it often beats Ceph-backed swap, and can be used together with it at a
higher priority.

**None of this reduces Odoo's memory use.** The actual root-cause fix is capping Odoo:
set `workers` appropriately for 2 vCPU and set `limit_memory_soft` / `limit_memory_hard`
so Odoo recycles a bloated worker itself. Then the RAM bump to `4CPU-8GRAM-0DISK`
(8 GiB / 4 vCPU — confirmed to fit on `rc3-c-1`, ~17.1 GiB free) is worth doing.

---

## 8. Failure modes and recovery

| Symptom | Cause | Fix |
|---|---|---|
| Boot drops to **emergency mode** | fstab entry for a missing device without `nofail` | At the console: `mount -o remount,rw /`, then `cp /etc/fstab.bak-<date> /etc/fstab`, reboot. Re-add with `nofail`. |
| Boot stalls ~90 s then continues | missing `x-systemd.device-timeout` | Add `x-systemd.device-timeout=10s`. |
| `swapon --show` empty after reboot | UUID changed (disk re-made) or volume detached | `sudo blkid` for the current UUID; check Cinder: `openstack volume show 56e6afce-…` should be `in-use`. Update fstab. |
| `swapon: … insecure permissions` | swap file/partition world-readable | `sudo chmod 0600` on the target. |
| Swap active but VM still hangs under load | swap on slow Ceph is doing its worst | Install `earlyoom` (§7); cap Odoo memory. |
| Wrong disk wiped | `/dev/vdb` used instead of `by-id` | Boot volume is unrecoverable without a backup — see `docs/BACKUP_RECOVERY.md`. **This is why §2 exists.** |

### Rollback

```bash
sudo swapoff -a
sudo umount /mnt/data
sudo cp /etc/fstab.bak-<date> /etc/fstab
sudo systemctl daemon-reload
```

Then detach the volume cluster-side if you are abandoning the change:

```bash
openstack --os-cloud admin server remove volume \
  ca90c0bc-a8c0-4060-8a6b-0aaddc6abaa1 56e6afce-ae83-4e39-88db-b5306aa511ca
```

The volume has `delete_on_termination: False`, so it survives detach and VM deletion.
