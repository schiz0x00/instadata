# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in instadata, please report it privately.

**Do not open a public GitHub issue.** Instead, send a detailed report via the **Security** tab at:

https://github.com/schiz0x00/instadata/security/advisories/new

You should receive a response within 48 hours. If you do not, please follow up.

## Scope

instadata handles session cookies, proxy credentials and untrusted responses
from a remote service, and writes files to paths derived from that response
data. In-scope: leaking cookies, proxy URLs or other credentials into logs,
exception text or the `--json` report; path traversal or arbitrary writes via
attacker-controlled usernames, shortcodes or media URLs; and code execution
reachable from a malicious API response.

Out of scope: rate limiting or blocking by Instagram, breakage caused by
upstream API changes, and anything that requires the operator to supply a
hostile cookie file or proxy of their own.

## Supported Versions

Only the latest release on PyPI receives security patches.
