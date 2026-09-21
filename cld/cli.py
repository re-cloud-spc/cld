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
from cld.steps import (current_project, select_az, select_flavor, select_image,
                       select_network, security_review, flavor_topology,
                       flavor_warnings)
from cld.storage import attach_storage
from cld.deletevol import delete_volume
from cld.listcmd import run_list, RESOURCES
from cld.ui import out, warn, err, confirm, prompt_str, Abort
from cld.vm import build_payload, create_vm, print_summary
from cld.answers import save_answers, load_answers

SUBCOMMANDS = {"init", "createvm", "attachstorage", "deletevolume", "check", "list"}


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


def build_parser():
    ap = argparse.ArgumentParser(
        prog="cld",
        description="Interactive Management Tool for Re:Cloud and NanoCloud Admins",
        allow_abbrev=False)
    sub = ap.add_subparsers(dest="command")

    p_init = sub.add_parser(
        "init", help="mint an app credential + write a clouds.yaml entry",
        allow_abbrev=False)
    p_init.add_argument("--project", required=True,
                        help="project to scope the application credential to")
    p_init.add_argument("--cloud", help="clouds.yaml entry name (default: project)")
    p_init.add_argument("--name", default="cld",
                        help="application credential name (default: cld)")
    p_init.add_argument("--admin-role", action="store_true",
                        help="grant the admin role on the project first (for "
                             "cluster-wide inventory visibility)")

    p_create = sub.add_parser("createvm", help="provision a VM (no data volume)",
                              allow_abbrev=False)
    p_create.add_argument("--cloud", help="cloud name from clouds.yaml")
    p_create.add_argument("--dry-run", action="store_true",
                          help="walk the wizard and print the payload; change "
                               "nothing")
    p_create.add_argument("--save-answers", metavar="FILE",
                          help="save the collected spec to FILE for later replay")
    p_create.add_argument("--non-interactive", metavar="FILE",
                          help="replay a previously saved spec file")

    p_attach = sub.add_parser(
        "attachstorage", help="create + attach a data volume to an existing server",
        description="Create a new Cinder volume (or, with --disk, take an existing\n"
                    "one) and attach it to an EXISTING server. Never creates or\n"
                    "touches the server itself; a failed attach can only roll back\n"
                    "the volume, and that rollback defaults to No.",
        epilog=_ATTACH_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
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
        epilog=_DELETE_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False)
    p_del.add_argument("--cloud", help="cloud name from clouds.yaml")
    p_del.add_argument("--volumeid", required=True, metavar="VOLUME_ID",
                       help="full UUID of the volume to delete; refused if it is "
                            "attached, in a transitional state, or has snapshots")
    p_del.add_argument("--dry-run", action="store_true",
                       help="show the report and stop; delete nothing")

    p_check = sub.add_parser(
        "check", help="authenticate, print the scoped project/user, and exit",
        allow_abbrev=False)
    p_check.add_argument("--cloud", help="cloud name from clouds.yaml")

    p_list = sub.add_parser(
        "list", help="show read-only inventory of the cloud",
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
                        metavar="0|1",
                        help="volumes: 1 = only available (unattached) volumes, "
                             "0 = only unavailable ones; default is all")

    return ap


# --------------------------------------------------------------------------- #
# createvm orchestration
# --------------------------------------------------------------------------- #
def run_interactive(cloud):
    conn = connect(cloud)
    project_id, project_name = current_project(conn)

    inv = Inventory(conn)
    az = select_az(conn, inv)
    flavor = select_flavor(conn, inv)
    if not flavor:
        err("a flavor is required")
        sys.exit(1)
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
    name = prompt_str("VM name", default=None)

    spec = {
        "name": name,
        "cloud": cloud,
        "project_id": project_id,
        "project_name": project_name,
        "az": az,
        "flavor_id": flavor.id, "flavor_name": flavor.name,
        "image_id": image.id, "image_name": image.name,
        "network": network,
        "security": security,
    }
    return conn, spec


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


def cmd_createvm(args):
    cloud = select_cloud(args.cloud)
    audit.audit("createvm.start", cloud=cloud, dry_run=args.dry_run)
    if args.non_interactive:
        spec = load_answers(args.non_interactive)
        spec.setdefault("cloud", cloud)
        conn = connect(cloud)
        # This path skips select_flavor, so a saved spec pinned to a flavor with
        # unsatisfiable NUMA specs would hit the same create-time 400. Same
        # check, same default-to-No gate.
        if not _check_saved_flavor(conn, spec, args.dry_run):
            out("Aborted; nothing created.")
            return 0
    else:
        conn, spec = run_interactive(cloud)

    print_summary(spec)

    if args.save_answers:
        save_answers(args.save_answers, spec)

    if args.dry_run:
        out()
        out("[yellow]--dry-run: no resources created.[/yellow]")
        out("[dim]create payload:[/dim]")
        out(str(build_payload(spec, conn=conn)))
        return 0

    if not confirm("Create this VM now?", default=False):
        out("Aborted; nothing created.")
        return 0

    create_vm(conn, spec)
    return 0


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
