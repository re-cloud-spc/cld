"""cld command-line entry point.

Subcommands: init | createvm | attachstorage | deletevolume | check | list.
For back-compat / muscle memory, a bare invocation (or leading flags) defaults to
`createvm`, e.g. `cld --dry-run` == `cld createvm --dry-run`.
"""

import argparse
import sys

from cld import audit
from cld.cloud import connect, select_cloud, run_check
from cld.inventory import Inventory
from cld.hosts import host_capacity, host_fit, select_host
from cld.disks import select_disks, recheck_disks, print_attach_commands
from cld.steps import (current_project, select_az, select_flavor, select_image,
                       select_network, security_review, flavor_topology,
                       flavor_warnings)
from cld.storage import attach_storage
from cld.deletevol import delete_volume
from cld.listcmd import run_list, RESOURCES
from cld.ui import out, warn, err, confirm, prompt_str, Abort
from cld.vm import build_payload, create_vm, print_summary
from cld.answers import save_answers, load_answers

try:
    # Optional, like rich: coloured --help when installed, plain argparse otherwise.
    from rich_argparse import RawDescriptionRichHelpFormatter

    class _HelpFormatter(RawDescriptionRichHelpFormatter):
        # Help/epilog text is plain prose with literal brackets ([y/N],
        # [resource]); colour the structure, never parse it as rich markup.
        help_markup = False
        text_markup = False
except ImportError:
    _HelpFormatter = argparse.RawDescriptionHelpFormatter

SUBCOMMANDS = {"init", "createvm", "attachstorage", "deletevolume", "check", "list",
               "completion"}


_ATTACH_EPILOG = """\
after a successful attach:
  cld prints the in-guest steps to format/mount the disk. It names the disk by
  its exact path, /dev/disk/by-id/virtio-<first 20 chars of the volume ID>;
  Nova's /dev/vdX is only a hint (the guest may name it differently).
  cld never mounts anything itself.

bootable / image volumes (an old VM's root disk):
  they carry the labels cloudimg-rootfs / UEFI / BOOT that the VM itself boots
  by, so its next reboot may come up on the ATTACHED disk. cld warns before
  and after the attach: do not reboot until `ls -l /dev/disk/by-label/` inside
  the VM points only at its own disk (reformat or detach the volume).

examples:
  cld attachstorage --cloud admin --serverid <server-id> --size 50 --dry-run
  cld attachstorage --cloud admin --serverid <server-id> --disk <volume-id>
"""

_DELETE_EPILOG = """\
refused, with no override, when the volume:
  - is attached: Cinder attachment, a Nova server still listing it, or its
    Ceph image held open by a client. Detach it first: stop use + umount
    inside the VM, remove its /etc/fstab line, then
    `openstack server remove volume <server> <volume>`, then re-run.
    cld never detaches anything itself.
  - has a status other than available / error (reserved, attaching,
    detaching, error_deleting, ... each get a hint)
  - belongs to another project than this cloud's credential
  - has Cinder snapshots (cld never cascades)

report shown before you decide:
  Cinder facts (size, type, source image/snapshot/volume, age, snapshots,
  backups, clones), the contents read-only from Ceph (bytes ever written,
  partitions, filesystems + used space, ext4 last mount time/path) and the
  history (RBD timestamps, every cld audit-log line for the volume).
  Reading contents needs the Ceph keyring (root, or `sudo -n`); without it
  the report says so and you decide on metadata alone.

deciding:
  answer y (default No; Enter, q or Ctrl-D keep the volume), then type the
  first 8 characters of the volume ID. cld re-reads the volume, refuses if it
  changed meanwhile, deletes without force, logs it, and verifies it is gone.
  Backups of the volume are separate and remain.

examples:
  cld deletevolume --cloud admin --volumeid <uuid> --dry-run   # report only
  cld deletevolume --cloud admin --volumeid <uuid>
"""


_CREATE_EPILOG = """\
wizard questions (Enter = default; q or Ctrl-D quits, creates nothing;
menus number from 0):
   1. cloud (project): only if no --cloud and >1 clouds.yaml entry
   2. availability zone: only if >1 AZ; Enter lets the scheduler decide
   3. flavor: required; one that cannot boot needs an extra yes
   4. compute host: only hosts with room; Enter = scheduler decides
   5. image: required
   6. network/subnet: required; an external network needs an extra yes
   7. SSH keypairs: several allowed (e.g. 0,2); Enter = none (risky)
   8. security groups: repeat, Enter ends; world-open groups need a yes
   9. floating IP: default No; a yes needs a second confirmation
  10. data disks: available ones, by #; bootable ones need a yes
  11. VM name: required, free text
  12. Create this VM now?: default No; skipped with --dry-run
  Disks are never attached by createvm: once the VM is ACTIVE it prints
  one `cld attachstorage --disk <full-id>` line per disk (re-checked).

compute host (admin; shown after the flavor):
  a table of every compute host in the chosen AZ with free vCPU / RAM from
  Placement, the RAM left after this flavor, and whether it fits. Press
  Enter to let the scheduler decide (the default; it already prefers the
  host with the most free RAM). Or pick one of the hosts that fit: the pin is
  sent as Nova's `host` field (API 2.74), which still runs the scheduler and
  the Placement claim, so a full host is refused, never overcommitted. There
  is no round-robin placement. See the same table any time with
  `cld list hosts`.

--host HOST:
  pins without the menu and skips the AZ question (the host's AZ is
  used). An unknown or down host stops before any question. If the
  flavor does not fit the host, a dry run only warns; a real run asks
  "Proceed anyway?" (default No). Overrides the host of a saved spec.

--non-interactive FILE:
  re-checks the saved flavor and the saved host against current capacity.
  If the host no longer fits, a dry run only warns; a real run asks
  "Proceed anyway?" (default No).

examples:
  cld list hosts --cloud admin                      # where is there room?
  cld createvm --cloud <project> --dry-run          # walk it, create nothing
  cld createvm --cloud <project> --save-answers vm.yaml
  cld createvm --cloud admin --host rc3-c-3 --dry-run
  cld createvm --non-interactive vm.yaml --dry-run
"""

_LIST_EPILOG = """\
resources:
  clouds    clouds.yaml entries (no --cloud or auth needed)
  servers   name, ID, status, flavor, IP, AZ (default)
  volumes   Cinder volumes + the VM each is attached to; filter with --available
  flavors   vCPU / RAM / root disk + how many servers use each
  images    size, min disk/RAM, visibility + how many servers use each
  networks  usable networks/subnets, CIDR, gateway, external flag
  azs       per-AZ capacity (summed from Placement) + the per-host table
  hosts     per compute host: AZ, service state, server count, vCPU and RAM
            used/cap/free from Placement -- the numbers the scheduler claims
            against (admin; cap = (total - reserved) x allocation ratio;
            SHUTOFF servers still hold their allocation)
  capacity  Cinder (Ceph) pool capacity + this project's volume quota

Everything here is read-only and is not written to the audit log.

examples:
  cld list hosts --cloud admin
  cld list volumes --cloud admin --all-projects --available 1
"""


_COMPLETION_EPILOG = """\
completes `cld`, `cld.py` / `./cld.py`, `python3 cld.py` and `python3 -m cld`;
plain `python3 <TAB>` keeps its usual completion.

install (once; regenerate after upgrading cld):
  python3 cld.py completion bash > ~/.local/share/cld/completion.bash
  echo 'source ~/.local/share/cld/completion.bash' >> ~/.bashrc

or just for this shell:
  source <(python3 cld.py completion bash)
"""


def build_parser():
    ap = argparse.ArgumentParser(
        prog="cld",
        description="Interactive Management Tool for Re:Cloud and NanoCloud Admins",
        formatter_class=_HelpFormatter,
        allow_abbrev=False)
    sub = ap.add_subparsers(dest="command")

    p_init = sub.add_parser(
        "init", help="mint an app credential + write a clouds.yaml entry",
        formatter_class=_HelpFormatter, allow_abbrev=False)
    p_init.add_argument("--project", required=True,
                        help="project to scope the application credential to")
    p_init.add_argument("--cloud", help="clouds.yaml entry name (default: project)")
    p_init.add_argument("--name", default="cld",
                        help="application credential name (default: cld)")
    p_init.add_argument("--admin-role", action="store_true",
                        help="grant the admin role on the project first (for "
                             "cluster-wide inventory visibility)")

    p_create = sub.add_parser(
        "createvm", help="provision a VM (no data volume)",
        description="Interactive wizard that provisions ONE VM, showing live\n"
                    "capacity at every step. Optionally pins it to a compute host.\n"
                    "Creates nothing until you confirm (default No).",
        epilog=_CREATE_EPILOG, formatter_class=_HelpFormatter,
        allow_abbrev=False)
    p_create.add_argument("--cloud", help="cloud name from clouds.yaml")
    p_create.add_argument("--dry-run", action="store_true",
                          help="walk the wizard and print the payload; change "
                               "nothing")
    p_create.add_argument("--save-answers", metavar="FILE",
                          help="save the collected spec to FILE for later replay")
    p_create.add_argument("--non-interactive", metavar="FILE",
                          help="replay a previously saved spec file")
    p_create.add_argument("--host", metavar="HOST",
                          help="pin to this compute host (admin; e.g. rc3-c-3): "
                               "skips the AZ and host questions; checked "
                               "against Placement first")

    p_attach = sub.add_parser(
        "attachstorage", help="create + attach a data volume to an existing server",
        description="Create a new Cinder volume (or, with --disk, take an existing\n"
                    "one) and attach it to an EXISTING server. Never creates or\n"
                    "touches the server itself; a failed attach can only roll back\n"
                    "the volume, and that rollback defaults to No.",
        epilog=_ATTACH_EPILOG, formatter_class=_HelpFormatter,
        allow_abbrev=False)
    p_attach.add_argument("--cloud", help="cloud name from clouds.yaml")
    p_attach.add_argument("--serverid", help="target server ID "
                          "(otherwise you're prompted)")
    p_attach.add_argument("--disk", metavar="VOLUME_ID",
                          help="attach an existing volume by ID instead of "
                               "creating one; attaches only if it's available, "
                               "unattached and in this project (a bootable one "
                               "needs an extra yes)")
    p_attach.add_argument("--size", type=int, help="volume size in GB "
                          "(not allowed with --disk)")
    p_attach.add_argument("--type", dest="type_name", help="volume type "
                          "(not allowed with --disk)")
    p_attach.add_argument("--dry-run", action="store_true",
                          help="show what would be created/attached; change nothing")

    p_del = sub.add_parser(
        "deletevolume", help="permanently delete ONE unattached volume, after "
        "showing its contents/age/history",
        description="Permanently delete ONE unattached Cinder volume -- but first\n"
                    "show what it holds, how old it is and its history, and let\n"
                    "you decide. Nothing is deleted without two confirmations.",
        epilog=_DELETE_EPILOG, formatter_class=_HelpFormatter,
        allow_abbrev=False)
    p_del.add_argument("--cloud", help="cloud name from clouds.yaml")
    p_del.add_argument("--volumeid", required=True, metavar="VOLUME_ID",
                       help="full UUID of the volume to delete; refused if it is "
                            "attached, in a transitional state, or has snapshots")
    p_del.add_argument("--dry-run", action="store_true",
                       help="show the report and stop; delete nothing")

    p_check = sub.add_parser(
        "check", help="authenticate, print the scoped project/user, and exit",
        formatter_class=_HelpFormatter, allow_abbrev=False)
    p_check.add_argument("--cloud", help="cloud name from clouds.yaml")

    p_list = sub.add_parser(
        "list", help="show read-only inventory of the cloud",
        description="Show read-only inventory of the cloud. Makes no changes.",
        epilog=_LIST_EPILOG, formatter_class=_HelpFormatter,
        allow_abbrev=False)
    p_list.add_argument("resource", nargs="?", default="servers",
                        choices=RESOURCES,
                        help="what to list (default: servers)")
    p_list.add_argument("--cloud", help="cloud (= project) from clouds.yaml "
                        "(not needed for 'clouds')")
    p_list.add_argument("--all-projects", action="store_true",
                        help="servers/volumes: include every project (admin); "
                             "default is the current project")
    p_list.add_argument("--available", type=int, choices=[0, 1], default=None,
                        nargs="?", const=1, metavar="0|1",
                        help="volumes: 1 (or bare --available) = only available "
                             "(unattached) volumes, 0 = only unavailable ones; "
                             "default is all")

    p_comp = sub.add_parser(
        "completion", help="print a shell completion script",
        description="Print a bash completion script for cld (subcommands, flags,\n"
                    "list resources, --cloud names from clouds.yaml). Local only:\n"
                    "no connection, nothing logged.",
        epilog=_COMPLETION_EPILOG, formatter_class=_HelpFormatter,
        allow_abbrev=False)
    p_comp.add_argument("shell", nargs="?", default="bash", choices=["bash"],
                        help="shell to generate for (default: bash)")

    return ap


# --------------------------------------------------------------------------- #
# createvm orchestration
# --------------------------------------------------------------------------- #
def run_interactive(cloud, host=None, dry_run=False):
    conn = connect(cloud)
    project_id, project_name = current_project(conn)

    inv = Inventory(conn)
    if host:
        az = _resolve_host(conn, inv, host)["az"]
        out(f"Pinned by --host: [bold]{host}[/bold] (AZ {az or 'unknown'})")
    else:
        az = select_az(conn, inv)
    flavor = select_flavor(conn, inv)
    if not flavor:
        err("a flavor is required")
        sys.exit(1)
    if host:
        if not _host_fits(conn, inv, host, flavor, dry_run):
            raise Abort()
    else:
        host = select_host(conn, inv, flavor, az)
    image = select_image(conn, inv)
    if not image:
        err("an image is required")
        sys.exit(1)
    if getattr(image, "min_disk", 0) and image.min_disk > flavor.disk:
        warn(f"image needs min_disk {image.min_disk} GB but flavor root disk is "
             f"{flavor.disk} GB - boot may fail.")
    network = select_network(conn, project_id)
    if not network:
        sys.exit(1)
    security = security_review(conn, project_id)
    disks = select_disks(conn, project_id, az)
    name = prompt_str("VM name", default=None)

    spec = {
        "name": name,
        "cloud": cloud,
        "project_id": project_id,
        "project_name": project_name,
        "az": az,
        "host": host,
        "flavor_id": flavor.id, "flavor_name": flavor.name,
        "image_id": image.id, "image_name": image.name,
        "network": network,
        "security": security,
        "disks": disks,
    }
    return conn, spec


def _resolve_host(conn, inv, host):
    """--host: the host's Placement row, or exit 1 before any question.

    Only a known, up compute host is accepted; there is no "pin anyway" for a
    name Placement does not know. -> row, with "az" None when it is unknown.
    """
    rows = host_capacity(conn, inv)
    if not rows:
        err("--host needs per-host data from Placement (the admin role)")
        sys.exit(1)
    row = next((r for r in rows if r["host"] == host), None)
    if row is None:
        err(f"--host {host}: not a compute host here; known: "
            f"{', '.join(r['host'] for r in rows)}")
        sys.exit(1)
    if row["state"] != "up":
        err(f"--host {host}: nova-compute is {row['state']}")
        sys.exit(1)
    return dict(row, az=None if row["az"] == "?" else row["az"])


def _host_fits(conn, inv, host, flavor, dry_run):
    """Placement check of the --host pin for this flavor. -> True to go on.
    Same gate as a saved host: a dry run only warns, a real run defaults No."""
    row = next((r for r in host_capacity(conn, inv) if r["host"] == host), None)
    reason = host_fit(row, flavor) if row else "no longer listed by Placement"
    if not reason:
        return True
    warn(f"--host {host} has no room for '{flavor.name}': {reason}. Nova "
         "would refuse it with 'No valid host'.")
    if dry_run:
        return True
    return confirm("Proceed anyway?", default=False)


def _check_saved_flavor(conn, spec, dry_run):
    """Warn about a saved spec's flavor topology. -> True to keep going.

    Read-only: resolves the flavor id from the spec and reuses the wizard's
    checker. A dry run only reports; a real run needs an explicit yes.
    """
    flavor_id = spec.get("flavor_id")
    if not flavor_id:
        return True
    try:
        flavor = conn.compute.get_flavor(flavor_id)
    except Exception:  # noqa: BLE001 - missing/unreadable flavor surfaces later
        return True
    for msg in flavor_warnings(flavor):
        warn(msg)
    _, blocker = flavor_topology(flavor)
    if not blocker:
        return True
    warn(f"saved flavor '{getattr(flavor, 'name', flavor_id)}': {blocker}")
    if dry_run:
        return True
    return confirm("Proceed anyway?", default=False)


def _check_saved_host(conn, spec, dry_run):
    """Re-check a saved spec's pinned host against Placement NOW. -> True to go on.

    The room that was there when the spec was saved may be gone. Nova would
    refuse anyway (the 2.74 host request still runs the scheduler), but only
    after cld has reserved a port. A dry run only reports; a real run on a host
    that no longer fits needs an explicit yes.
    """
    host = spec.get("host")
    if not host:
        return True
    try:
        flavor = conn.compute.get_flavor(spec["flavor_id"])
    except Exception:  # noqa: BLE001 - missing flavor surfaces at create time
        return True
    row = next((r for r in host_capacity(conn) if r["host"] == host), None)
    if row is None:
        warn(f"saved host '{host}' is not a known compute host (or Placement "
             "is unreadable without the admin role)")
    else:
        reason = host_fit(row, flavor)
        if not reason:
            return True
        warn(f"saved host '{host}' has no room for '{flavor.name}': {reason}")
    if dry_run:
        return True
    return confirm("Proceed anyway?", default=False)


def cmd_createvm(args):
    cloud = select_cloud(args.cloud)
    audit.audit("createvm.start", cloud=cloud, dry_run=args.dry_run,
                host=args.host)
    if args.non_interactive:
        spec = load_answers(args.non_interactive)
        spec.setdefault("cloud", cloud)
        conn = connect(cloud)
        if args.host:
            az = _resolve_host(conn, None, args.host)["az"]
            if spec.get("host") != args.host or spec.get("az") != az:
                out(f"--host overrides the saved placement: host "
                    f"{spec.get('host') or '(scheduler)'} -> {args.host}, "
                    f"AZ {spec.get('az') or '(scheduler)'} -> {az or '(scheduler)'}")
            spec["host"], spec["az"] = args.host, az
        # This path skips select_flavor, so a saved spec pinned to a flavor with
        # unsatisfiable NUMA specs would hit the same create-time 400. Same
        # check, same default-to-No gate.
        if not (_check_saved_flavor(conn, spec, args.dry_run)
                and _check_saved_host(conn, spec, args.dry_run)):
            out("Aborted; nothing created.")
            return 0
    else:
        conn, spec = run_interactive(cloud, args.host, args.dry_run)

    print_summary(spec)

    if args.save_answers:
        save_answers(args.save_answers, spec)

    if args.dry_run:
        out()
        out("[yellow]--dry-run: no resources created.[/yellow]")
        out("[dim]create payload:[/dim]")
        out(str(build_payload(spec, conn=conn)))
        _planned_disks(conn, spec)
        return 0

    if not confirm("Create this VM now?", default=False):
        out("Aborted; nothing created.")
        return 0

    server = create_vm(conn, spec)
    if server is not None:
        _planned_disks(conn, spec, server.id)
    elif spec.get("disks"):
        out("[dim]The planned data disks were not touched.[/dim]")
    return 0


def _planned_disks(conn, spec, server_id=None):
    """Re-read the planned disks (read-only) and print an attachstorage line for
    each one still available. createvm itself never attaches (rc3 #112)."""
    if not spec.get("disks"):
        return
    project_id = spec.get("project_id") or getattr(conn, "current_project_id", None)
    print_attach_commands(recheck_disks(conn, spec["disks"], project_id),
                          spec.get("cloud"), server_id)


def cmd_attachstorage(args):
    cloud = select_cloud(args.cloud)
    attach_storage(cloud, server_arg=args.serverid, size=args.size,
                   type_name=args.type_name, disk=args.disk,
                   dry_run=args.dry_run)
    return 0


def cmd_deletevolume(args):
    return delete_volume(select_cloud(args.cloud), args.volumeid,
                         dry_run=args.dry_run)


def cmd_check(args):
    return run_check(select_cloud(args.cloud))


def cmd_list(args):
    if args.available is not None and args.resource != "volumes":
        err("--available applies only to `list volumes`")
        return 2
    return run_list(args.resource, args.cloud, args.all_projects,
                    available=args.available)


def cmd_completion(args):
    from cld.completion import bash_script
    sys.stdout.write(bash_script(build_parser()))
    return 0


def cmd_init(args):
    from cld.init import init_command
    audit.audit("init.start", project=args.project, cloud=args.cloud or args.project)
    return init_command(args)


DISPATCH = {
    "init": cmd_init,
    "createvm": cmd_createvm,
    "attachstorage": cmd_attachstorage,
    "deletevolume": cmd_deletevolume,
    "check": cmd_check,
    "list": cmd_list,
    "completion": cmd_completion,
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Default to createvm for a bare invocation or leading flags.
    if not argv or (argv[0] not in SUBCOMMANDS and argv[0] not in ("-h", "--help")):
        argv = ["createvm"] + argv

    args = build_parser().parse_args(argv)
    handler = DISPATCH.get(args.command)
    if handler is None:  # e.g. argparse printed help with no command
        build_parser().print_help()
        return 1
    try:
        rc = handler(args)
    except KeyboardInterrupt:
        out("\nInterrupted; nothing created or deleted.")
        return 130
    except Abort:
        # q/quit/cancel or Ctrl-D at any prompt. Every prompt runs before the
        # create/attach calls, so this can only unwind a spec that was never
        # submitted -- and rollback prompts use confirm_destructive(), which
        # maps Abort to False rather than letting it reach here.
        out("\nAborted; nothing created or deleted.")
        return 130
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
