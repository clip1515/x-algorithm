# Security Policy

This repository is an unofficial mirror that tracks [`xai-org/x-algorithm`](https://github.com/xai-org/x-algorithm), the upstream open-source repository for X's recommendation algorithm. Its own automation is limited to periodically syncing content from upstream and opening an issue when upstream changes.

## Reporting a vulnerability

Where you report an issue depends on where it lives:

- **Vulnerability in the algorithm/service code itself** (Scala, Rust, Python, Java sources under directories such as `home-mixer`, `phoenix`, `bdsm`, `thunder`, etc.): this code is maintained upstream, not in this mirror. Please report it directly to X via their [HackerOne program](https://hackerone.com/x) or, if unavailable, to the upstream repository at [`xai-org/x-algorithm`](https://github.com/xai-org/x-algorithm). Do not open a public issue with exploit details.
- **Vulnerability in this mirror's own automation** (the GitHub Actions workflows in `.github/workflows/`, the upstream-sync state file, or any script added specifically to this fork): please open a [private security advisory](../../security/advisories/new) on this repository, or a GitHub issue if advisories are unavailable, describing the issue and, if possible, a reproduction.

Please do not disclose suspected vulnerabilities publicly (including in this repository's issue tracker) before they have had a chance to be triaged.

## Scope

Security tooling in this repository (dependency updates, secret scanning, static analysis) covers this fork's own configuration and the mirrored source tree as a courtesy scan. It is not a substitute for upstream's own security review process, and findings here that trace back to upstream code should still be reported per the mirror-vs-upstream split above.
