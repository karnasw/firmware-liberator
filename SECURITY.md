# Security

## Reporting a problem

Please report a security problem privately, through GitHub:
**[Report a vulnerability](https://github.com/adamsthws/firmware-liberator/security/advisories/new)**.
Do not open an issue or a discussion, where it would be public before it can
be fixed. Say what you found, how to see it, and what it lets someone do. You
will get an answer as soon as possible, saying what will be done and when.

## What is in scope

- **The conversion tool**: everything in this repository that runs on your
  computer or is sent to the device.
- **The firmware files it installs** from `artifacts/`, as built by this project.
- **The website and the project's API**:
  [firmwareliberator.com](https://firmwareliberator.com), and
  `api.firmwareliberator.com`, which receives the reports and readouts the
  tool sends and the email addresses people leave to hear about a device.
  Both are built from `website/`.
- **The repository's workflows**, which update, release and deploy it.

Some of what crosses your local network during a conversion is sent in plain
HTTP; this is known, and
[docs/technical-details.md](docs/technical-details.md#what-crosses-the-network)
says what, when and why. A problem in Tasmota, ESPHome or OpenBeken itself
belongs with that project: [Tasmota](https://github.com/arendst/Tasmota/security),
[ESPHome](https://github.com/esphome/esphome/security) or
[OpenBeken](https://github.com/openshwprojects/OpenBK7231T_App), and one in
Cloudflare, which runs the website and the API, belongs with
[Cloudflare](https://www.cloudflare.com/disclosure/).

## Versions

Fixes go into the next release only, not into earlier ones. The website and
the API run only their latest version, so a fix to either takes effect once it
is deployed.
