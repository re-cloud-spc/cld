"""cld command-line entry point.

Subcommands: init | createvm | attachstorage | check.
For back-compat / muscle memory, a bare invocation (or leading flags) defaults to
`createvm`, e.g. `cld --dry-run` == `cld createvm --dry-run`.
"""

import argparse
import sys

from cld import audit
from cld.cloud import connect, select_cloud, run_check
from cld.inventory import Inventory
from cld.steps import (current_project, select_az, select_flavor, select_image,
                       select_network, security_review)
from cld.storage import attach_storage
from cld.listcmd import run_list, RESOURCES
from cld.ui import out, warn, err, confirm, prompt_str
from cld.vm import build_payload, create_vm, print_summary
from cld.answers import save_answers, load_answers

SUBCOMMANDS = {"init", "createvm", "attachstorage", "check", "list"}


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
        allow_abbrev=False)
    p_attach.add_argument("--cloud", help="cloud name from clouds.yaml")
    p_attach.add_argument("--serverid", help="target server ID "
                          "(otherwise you're prompted)")
    p_attach.add_argument("--disk", metavar="VOLUME_ID",
                          help="attach an existing volume by ID instead of "
                               "creating one; attaches only if it's available "
                               "and unattached")
    p_attach.add_argument("--size", type=int, help="volume size in GB "
                          "(not allowed with --disk)")
    p_attach.add_argument("--type", dest="type_name", help="volume type "
                          "(not allowed with --disk)")
    p_attach.add_argument("--dry-run", action="store_true",
                          help="show what would be created/attached; change nothing")

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
    security = security_review(conn)
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


def cmd_createvm(args):
    cloud = select_cloud(args.cloud)
    audit.audit("createvm.start", cloud=cloud, dry_run=args.dry_run)
    if args.non_interactive:
        spec = load_answers(args.non_interactive)
        spec.setdefault("cloud", cloud)
        conn = connect(cloud)
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


def cmd_check(args):
    return run_check(select_cloud(args.cloud))


def cmd_list(args):
    return run_list(args.resource, args.cloud, args.all_projects)


def cmd_init(args):
    from cld.init import init_command
    audit.audit("init.start", project=args.project, cloud=args.cloud or args.project)
    return init_command(args)


DISPATCH = {
    "init": cmd_init,
    "createvm": cmd_createvm,
    "attachstorage": cmd_attachstorage,
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
        out("\nInterrupted; nothing created.")
        return 130
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
