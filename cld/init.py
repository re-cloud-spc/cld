"""init: one-time-per-project bootstrap.

Mints an application credential and writes its clouds.yaml entry. Application
credentials are bound to a single project and cannot be re-scoped, so run this
once per project you want to deploy into. Each run adds one named entry to
~/.config/openstack/clouds.yaml (mode 600). The secret is shown only once by
Keystone; it is written straight into clouds.yaml and never printed.

Prerequisite: admin (or any password) credentials in the environment. On a Kolla
controller:  source <(sudo cat /root/admin-openrc.sh)
"""

import os
import sys

try:
    import openstack
    from openstack import exceptions as os_exc
except ImportError:
    sys.exit("openstacksdk is required: pip install openstacksdk")

try:
    import yaml
except ImportError:
    sys.exit("PyYAML is required: pip install PyYAML")

from cld import audit

CLOUDS_PATH = os.path.expanduser("~/.config/openstack/clouds.yaml")


def env_required(name):
    val = os.environ.get(name)
    if not val:
        sys.exit(
            f"{name} is not set. Load admin credentials first, e.g.:\n"
            f"    source <(sudo cat /root/admin-openrc.sh)"
        )
    return val


def connect_admin(project_name):
    """Password auth from the environment, re-scoped to project_name."""
    auth_url = env_required("OS_AUTH_URL")
    username = env_required("OS_USERNAME")
    password = env_required("OS_PASSWORD")
    return openstack.connect(
        auth_url=auth_url,
        username=username,
        password=password,
        project_name=project_name,
        user_domain_name=os.environ.get("OS_USER_DOMAIN_NAME", "Default"),
        project_domain_name=os.environ.get("OS_PROJECT_DOMAIN_NAME", "Default"),
        region_name=os.environ.get("OS_REGION_NAME"),
        interface=os.environ.get("OS_INTERFACE", "internal"),
        identity_api_version="3",
    )


def ensure_admin_role(conn, project):
    """Grant the admin role to the current user on this project (best effort)."""
    try:
        role = conn.identity.find_role("admin")
        user = conn.identity.find_user(os.environ["OS_USERNAME"],
                                       domain_id=conn.identity.find_domain(
                                           os.environ.get("OS_USER_DOMAIN_NAME",
                                                          "Default")).id)
        conn.identity.assign_project_role_to_user(project.id, user.id, role.id)
        audit.audit("role.assign", role="admin", user=user.name,
                    project=project.name)
        print(f"[ok] ensured 'admin' role for {user.name} on project "
              f"{project.name}")
    except os_exc.SDKException as e:
        print(f"[warn] could not assign admin role: {e}\n"
              f"       grant it manually if cluster-wide inventory is empty:\n"
              f"       openstack role add --user {os.environ.get('OS_USERNAME')} "
              f"--project {project.name} admin")


def merge_clouds_entry(name, entry):
    os.makedirs(os.path.dirname(CLOUDS_PATH), exist_ok=True)
    data = {}
    if os.path.isfile(CLOUDS_PATH):
        with open(CLOUDS_PATH) as f:
            data = yaml.safe_load(f) or {}
    clouds = data.setdefault("clouds", {})
    existed = name in clouds
    clouds[name] = entry
    # Write with restrictive perms from the start.
    fd = os.open(CLOUDS_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    os.chmod(CLOUDS_PATH, 0o600)
    return existed


def init_command(args):
    cloud_name = args.cloud or args.project

    try:
        conn = connect_admin(args.project)
        project = conn.identity.find_project(
            args.project,
            domain_id=conn.identity.find_domain(
                os.environ.get("OS_PROJECT_DOMAIN_NAME", "Default")).id)
        if project is None:
            sys.exit(f"project '{args.project}' not found")
    except os_exc.SDKException as e:
        sys.exit(f"could not authenticate / find project: {e}\n"
                 f"(does the admin user have a role on '{args.project}'? "
                 f"if not, run with --admin-role or grant one manually)")

    if args.admin_role:
        ensure_admin_role(conn, project)
        # reconnect so the new role is in the token used to create the app cred
        conn = connect_admin(args.project)

    user_id = conn.current_user_id
    try:
        appcred = conn.identity.create_application_credential(
            user=user_id, name=args.name,
            description=f"cld tool ({args.project})")
        audit.audit("appcred.create", id=appcred.id, name=args.name,
                    project=project.name)
    except os_exc.ConflictException:
        sys.exit(f"an application credential named '{args.name}' already exists "
                 f"for this user.\nDelete it first: "
                 f"openstack application credential delete {args.name}")
    except os_exc.SDKException as e:
        sys.exit(f"could not create application credential: {e}")

    entry = {
        "auth_type": "v3applicationcredential",
        "auth": {
            "auth_url": os.environ["OS_AUTH_URL"],
            "application_credential_id": appcred.id,
            "application_credential_secret": appcred.secret,
        },
        "region_name": os.environ.get("OS_REGION_NAME", "RegionOne"),
        "interface": os.environ.get("OS_INTERFACE", "internal"),
        "identity_api_version": 3,
    }
    existed = merge_clouds_entry(cloud_name, entry)
    audit.audit("clouds.entry", name=cloud_name, action="updated" if existed
                else "added")

    verb = "updated" if existed else "added"
    print(f"[ok] application credential '{args.name}' created for project "
          f"'{project.name}'")
    print(f"[ok] {verb} clouds.yaml entry '{cloud_name}' "
          f"({CLOUDS_PATH}, mode 600)")
    print()
    print("Test it (ignores the admin OS_* vars in this shell, so it just works):")
    print(f"    cld check --cloud {cloud_name}")
    print()
    print("Then create a VM:")
    print(f"    cld createvm --cloud {cloud_name} --dry-run")
    print()
    print("(The raw 'openstack --os-cloud' CLI would fail in THIS shell because")
    print(" the sourced OS_* vars clash with app-credential auth -- use check.)")
    return 0
