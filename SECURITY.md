# Security

Astrodoro runs on your own machine and talks to your own camera, so most of it
has no attack surface worth the name. Two parts do, and they are the ones worth
reporting on.

## What has a surface

- **The phone sensor server** (`src/astrodoro/pointing/handset.py`) opens a TLS
  port on your local network — 8443 by default — and serves a page plus a
  WebSocket. Anything on the same network can reach it. It carries orientation
  samples and nothing else: no filesystem access, no camera control. The
  certificate is self-signed and generated per machine.
- **Downloads**: the object catalogue (OpenNGC, over HTTPS from GitHub) and the
  DSS thumbnails. Both are fetched by explicit action and written under your
  data directory.

## Reporting

Please open a [private security advisory][advisory] rather than a public issue.
If you cannot, open a normal issue describing the *class* of problem without a
working exploit, and say that you have details to share privately.

There is no release cadence to promise against — this is one person's project.
Expect an acknowledgement within a week.

[advisory]: https://github.com/mathmed/astrodoro/security/advisories/new
