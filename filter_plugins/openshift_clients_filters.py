# -*- coding: utf-8 -*-
# Copyright: (c) 2026
# GNU General Public License v3.0+
"""Pick OpenShift client tarballs from a mirror.openshift.com release dir.

File naming differs by release, CPU architecture and glibc target, e.g.:
  4.14: openshift-client-linux.tar.gz, oc-mirror.tar.gz, opm-linux.tar.gz,
        openshift-install-rhel8-amd64.tar.gz (FIPS build)
  4.16+: also openshift-client-linux-amd64-rhel9.tar.gz, oc-mirror.rhel9.tar.gz,
        opm-linux-rhel9.tar.gz, openshift-install-rhel9-amd64.tar.gz
Older releases' sha256sum.txt also lists only the versioned names
(opm-linux-4.14.40.tar.gz), newer ones both. So instead of building names,
every client has an ordered list of candidate stems, each tried as
<stem>-<version>.tar.gz then <stem>.tar.gz, and the first one present in
the release's sha256sum.txt wins - newer/odd layouts only need another
candidate, not new logic.
"""

from __future__ import absolute_import, division, print_function
__metaclass__ = type

from ansible.errors import AnsibleFilterError

# uname -m -> the arch tag mirror file names use.
ARCH_TAGS = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64',
             'ppc64le': 'ppc64le', 's390x': 's390x'}

# What each client tarball must yield, as installed names.
CLIENT_BINARIES = {
    'oc': ['oc', 'kubectl'],
    'openshift-install': ['openshift-install'],
    'oc-mirror': ['oc-mirror'],
    'opm': ['opm'],
    'ccoctl': ['ccoctl'],
}


def parse_sha256sum(text):
    """sha256sum.txt content -> {filename: sha256}."""
    out = {}
    for line in (text or '').splitlines():
        parts = line.split()
        if len(parts) == 2:
            out[parts[1].lstrip('*')] = parts[0]
    return out


def rhel_targets(glibc_version):
    """Newest RHEL glibc target this host can run, newest first.
    RHEL 9 binaries need glibc >= 2.34, RHEL 8 >= 2.28."""
    try:
        major, minor = (int(x) for x in str(glibc_version).split('.')[:2])
    except ValueError:
        return []
    v = (major, minor)
    return [n for n, need in ((9, (2, 34)), (8, (2, 28))) if v >= need]


def client_candidates(client, arch, rhel, fips=False):
    """Candidates for the mirror dir of `arch` itself (pub/openshift-v4/<arch>/):
    there, un-suffixed names are that arch's native build (amd64 in x86_64/,
    arm64 in aarch64/), and other arches carry a suffix."""
    tag = ARCH_TAGS.get(arch, arch)
    c = []
    if client == 'oc':
        c += ['openshift-client-linux-%s-rhel%d.tar.gz' % (tag, n) for n in rhel]
        c += ['openshift-client-linux-%s.tar.gz' % tag, 'openshift-client-linux.tar.gz']
    elif client == 'openshift-install':
        # The rhelN builds are the FIPS-capable, dynamically linked ones:
        # first choice with fips, last resort without (some arches ship
        # only those, e.g. aarch64 4.20).
        rhel_builds = ['openshift-install-rhel%d-%s.tar.gz' % (n, tag) for n in rhel]
        c += rhel_builds if fips else []
        c += ['openshift-install-linux-%s.tar.gz' % tag, 'openshift-install-linux.tar.gz']
        c += [] if fips else rhel_builds
    elif client == 'oc-mirror':
        c += ['oc-mirror.rhel%d.tar.gz' % n for n in rhel] + ['oc-mirror.tar.gz']
    elif client == 'opm':
        c += ['opm-linux-rhel%d.tar.gz' % n for n in rhel] + ['opm-linux.tar.gz']
    elif client == 'ccoctl':
        c += ['ccoctl-linux-rhel%d.tar.gz' % n for n in rhel] + ['ccoctl-linux.tar.gz']
    else:
        raise AnsibleFilterError('Unknown OpenShift client %r (known: %s)'
                                 % (client, ', '.join(sorted(CLIENT_BINARIES))))
    return c


def pick_client_file(available, client, arch, glibc_version, version, fips=False, strict=True):
    """First candidate tarball for `client` present in `available`
    (dict or list of file names). Not found: raises with the candidates
    tried, or returns '' when strict=False."""
    names = set(available)
    candidates = []
    for name in client_candidates(client, arch, rhel_targets(glibc_version), fips):
        stem = name[:-len('.tar.gz')]
        candidates += ['%s-%s.tar.gz' % (stem, version), name]
    for name in candidates:
        if name in names:
            return name
    if not strict:
        return ''
    raise AnsibleFilterError('No %s tarball for %s/glibc %s%s in this release (tried: %s)'
                             % (client, arch, glibc_version, ' (fips)' if fips else '', ', '.join(candidates)))


def release_version(release_txt):
    """release.txt content -> the release it describes ('Name: 4.20.13')."""
    for line in (release_txt or '').splitlines():
        parts = line.split(':', 1)
        if len(parts) == 2 and parts[0].strip() == 'Name':
            return parts[1].strip()
    raise AnsibleFilterError('No "Name:" line in release.txt')


def client_binaries(client):
    return CLIENT_BINARIES[client]


class FilterModule(object):
    def filters(self):
        return {
            'parse_sha256sum': parse_sha256sum,
            'pick_client_file': pick_client_file,
            'client_binaries': client_binaries,
            'release_version': release_version,
        }
