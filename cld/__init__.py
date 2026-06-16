"""cld - interactive OpenStack tooling for a Ceph-backed cluster.

Subcommands (see cld.cli):
    init           one-time: mint an app credential + write a clouds.yaml entry
    createvm       provision a VM (boot from image onto the flavor root disk)
    attachstorage  create + attach a data volume to an existing server
    check          authenticate and print the scoped project/user, then exit

Every write action is recorded to logs/cld-<date>.log (see cld.audit).
"""

__version__ = "0.2.0"
