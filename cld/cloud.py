"""OpenStack connection + clouds.yaml discovery.

Auth is read ONLY from clouds.yaml: every connect() passes load_envvars=False so
leftover OS_* vars (e.g. from a sourced admin-openrc) can't inject a project
scope that application credentials reject ("cannot request a scope").
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
    yaml = None

from cld.ui import out, err, header, warn

CLOUDS_PATHS = [
    os.path.expanduser("~/.config/openstack/clouds.yaml"),
    "/etc/openstack/clouds.yaml",
    os.path.join(os.getcwd(), "clouds.yaml"),
]

CLOUDS_TEMPLATE = """\
No usable clouds.yaml was found.

Create ~/.config/openstack/clouds.yaml (chmod 600) with an application
credential (preferred over username/password - scoped and revocable). The
easiest way is:

    cld init --project <project>

Or write it by hand:

clouds:
  local:
    auth_type: v3applicationcredential
    auth:
      auth_url: https://<keystone-host>:5000/v3
      application_credential_id: <id>
      application_credential_secret: <secret>
    region_name: <region>
    interface: public
    identity_api_version: 3

Then:  chmod 600 ~/.config/openstack/clouds.yaml
"""


def list_cloud_names():
    if yaml is None:
        return []
    for path in CLOUDS_PATHS:
        if os.path.isfile(path):
            try:
                with open(path) as f:
                    data = yaml.safe_load(f) or {}
                return sorted((data.get("clouds") or {}).keys())
            except Exception:  # noqa: BLE001 - any parse issue -> treat as none
                return []
    return []


def select_cloud(arg_cloud):
    from cld.ui import choose
    names = list_cloud_names()
    if not names:
        out(CLOUDS_TEMPLATE)
        sys.exit(1)
    if arg_cloud:
        if arg_cloud not in names:
            err(f"cloud '{arg_cloud}' not in clouds.yaml ({', '.join(names)})")
            sys.exit(1)
        return arg_cloud
    if len(names) == 1:
        out(f"Using the only configured cloud: [bold]{names[0]}[/bold]")
        return names[0]
    header("Cloud (project)")
    out("[dim]Each cloud entry is an application credential bound to one "
        "project. Add more with `cld init`.[/dim]")
    return choose("Select cloud (project)", names)


def connect(cloud, project_id=None):
    # load_envvars=False: use ONLY clouds.yaml. Leftover OS_* vars (e.g. from a
    # sourced admin-openrc) would otherwise be layered on and inject a project
    # scope, which application credentials reject ("cannot request a scope").
    leftover = [k for k in os.environ if k.startswith("OS_")]
    if leftover:
        out(f"[dim]Note: {len(leftover)} OS_* env var(s) detected and ignored "
            f"(using clouds.yaml only).[/dim]")
    try:
        if project_id:
            return openstack.connect(cloud=cloud, project_id=project_id,
                                     load_envvars=False)
        return openstack.connect(cloud=cloud, load_envvars=False)
    except os_exc.SDKException as e:
        err(f"could not connect to cloud '{cloud}': {e}")
        sys.exit(1)


def safe_list(fn, *args, **kwargs):
    """Call an SDK lister, returning [] on any error (extension missing, RBAC)."""
    try:
        return list(fn(*args, **kwargs))
    except Exception as e:  # noqa: BLE001
        warn(f"could not read {getattr(fn, '__name__', 'resource')}: {e}")
        return []


def run_check(cloud):
    """Connectivity smoke test: authenticate and print scope, then exit.

    Immune to leftover OS_* env (connect() uses load_envvars=False), so it
    succeeds where a raw `openstack --os-cloud ...` call would fail with
    'Application credentials cannot request a scope'.
    """
    header("Credential check")
    conn = connect(cloud)
    try:
        pid = getattr(conn, "current_project_id", None)
        uid = getattr(conn, "current_user_id", None)
        name = None
        if pid:
            try:
                name = getattr(conn.identity.get_project(pid), "name", None)
            except Exception:  # noqa: BLE001
                name = None
        out(f"[green]OK[/green] cloud [bold]{cloud}[/bold] authenticated")
        out(f"  project: {name or '(name n/a)'}  ({pid or '?'})")
        out(f"  user:    {uid or '?'}")
        return 0
    except os_exc.SDKException as e:
        err(f"credential check failed: {e}")
        return 1
