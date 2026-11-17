# What the tool sends

Each time you run a conversion, the tool sends the project a short report of
how it went; each device you [read out](readouts.md) sends one too, as its
readout ends. Nothing in it says who you are, where you are, or which network
you are on.

Together, reports show which devices convert well, which fail and where, the
stages people choose to stop at, and which devices people want supported next: see the
**[stats page](https://firmwareliberator.com/stats)**.

## What a report NEVER holds

- Your name, email address or eWeLink account.
- Your Wi-Fi network's name or password, or any other password.
- Any address: your computer's, your router's, or the device's own or its
  hardware (MAC) address.
- The name you gave the device, your computer's name, or where you are.
- The time of day of the run: only the date the report arrived is kept.

## What a report holds

- **How the run ended:** converted, updated, put back on the firmware the
  device came from, or stopped; if stopped, at which stage, the kind of
  problem (such as "the device stopped answering"), and the tool's message
  about it, with any addresses taken out. If you stop it yourself, the stage
  you stopped at and how: q at a question, Ctrl-C, or closing the window.
  Nothing else is asked of you, and a run you stop before its first stage
  sends no report. If the tool itself went wrong, the place in its own code
  where it did (such as `KeyError at tools/autoflash/report.py:212`), never
  any of your values.
- **The device:** its kind and model (such as Sonoff S60, S60TPF), the
  version of Sonoff's firmware it had, the name its own updater checks an
  update against (and whether it is the one the tool expects), and on an
  Espressif device its chip's revision and its flash chip's maker code and size.
- **The conversion:** the firmware it went from and to, whether a backup of
  Sonoff's firmware was taken, whether the power meter kept its calibration,
  and, going back to Sonoff's firmware, where the eWeLink identity came from
  (the backup, the device or your account), never the identity itself.
- **A readout's report** (one for each device read out): how it ended -
  accepted by the website, kept on your computer unsent, not needed (one like
  it is in already), turned away as the website was full, not read as the
  device now runs other firmware, skipped, stopped by you, or failed, with
  the kind of problem - the device's kind, its model and the name and version
  of its Sonoff firmware, whether its whole memory was read, and how long it
  took. Never the readout itself, nor its reference.
- **Before a readout, the question whether one is wanted:** the device's
  model and the name and version of its Sonoff firmware, and the device as a
  report names it, so the website counts how many devices of each model
  people would read out, not how many times they asked.
- **How the run went:** how long it and each stage took, how long each stage
  waited for your answers, how long each stage kept the device busy, how
  often a stage was tried again, the device's Wi-Fi signal strength, and
  whether the run carried on an earlier one (and the tool's version that
  began it, if another), you typed your Wi-Fi details in (not the details
  themselves), your computer joined a network the device raised itself, the
  device had to be switched off and on (and how long it took to answer once
  back on) - each also by stage, as neither should be needed, and for the
  device's own network why: it was never given your Wi-Fi details, was given
  them but did not join, or had lost its settings - and whether the checks at the end passed (its relay, its
  firmware's release, its sensors' readings). Also how often
  you pressed Ctrl-C and then carried on.
- **The tool and your computer:** the tool's version, how you started it
  (`start.sh` or `start.cmd`) and whether from a download or a git clone,
  your operating system, its version (such as Windows 11 or Ubuntu 24.04) and
  your processor's kind (x86-64 or ARM), and the Python version the tool ran
  on, its own or one already on your computer. What may have got in the way
  on your computer's side: whether it reaches the device's network over Wi-Fi
  or Ethernet, whether a VPN was on, how many networks it is on, whether the
  tool added a firewall rule or asked for administrator rights, and whether
  it pointed out other security software (never which). The region of your
  eWeLink account (Europe, the Americas, Asia or China), if the run signed in to it. For a
  conversion, how many devices the tool has converted on your computer, this
  one included; for a readout, how many it has converted there so far:
  counted from its own records there, so deleting them starts the count
  again. Nothing that names your computer is sent.
- **For counting:** a random number, so a report sent twice counts once, and
  a mark telling a maintainer's test run from everyone else's. The number is
  the report's ID: when a run fails, the tool shows it where it says to open
  an issue, and puts it with the report on your clipboard to paste in, so the
  issue names its report. An issue is public: one that names a report lets
  anyone see that report's details beside it.

## How your device is recognised

So that a device converted twice counts as one, and a readout of it and its
conversion are known as the same device, the tool turns its hardware address
into a code on your computer with a one-way hash, and sends only that. A
readout takes the address from your eWeLink account's record of the device,
or where that has none, looks for the device on your network to learn it; one
it cannot find is sent with no code.
The server hashes it again with a secret key that never leaves it. What is
stored cannot be turned back into your device's address, and is linked to
nothing about you.

## Where it goes, and how long it is kept

The report goes to the project's server, `api.firmwareliberator.com`, on
Cloudflare. Like any website, it sees your internet address. Cloudflare logs
each request and keeps a trace of it - where it was
handled, and how long
each step took - for seven days, so that the maintainer can look into a
failure; neither is used for anything else. The server
itself uses your address only to limit how many reports one computer can
send: it holds the address in memory for a minute, and counts it for the day
as a scrambled code deleted after a week. So that a made-up model name cannot
be put on the public figures by one computer, it also notes, for each model a
report names, a scrambled code of your address and that model, which ties
you to nothing else; it is deleted once enough people have named that model,
or after a year. The server never stores the address itself.

- **Each report**, a conversion's or a readout's, is kept for as long as the
  project runs, so that how devices fare can be followed over the years.
  Nothing in it says who you are; it is the totals made from reports, never a
  report, that are published, such as "so many S60s converted in November".
- **Each device's record**, so it is still counted once, is kept for as long
  as the project runs: when it was first seen, how its first run ended, how
  many times it has been converted, and that count from the computer that
  first converted it.

## What it is used for

- **The public stats page**, which shows totals only, never a figure standing for
  fewer than five reports or devices, so no one report can be picked out. The
  figures are published under
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), free for anyone to
  use with credit.
- **Finding what to fix:** where runs stop, on which devices.
- **Choosing which devices to support next**, from how often each is seen.

Reports are never sold, shared, or used to contact you.

## Turning it off

- **For one run:** add `--no-report`, as in `sh start.sh run --no-report`
  (`start.cmd run --no-report` on Windows).
- **For every run:** add `AUTOFLASH_NO_REPORT=1` to the `.env` file in the
  tool's folder.

On its first run on a computer, the tool says it sends reports, linking to
this page. A dry run (`--dry-run`) and a run in the
[emulator](emulator.md) never send one.

## A report that could not be sent

If the report cannot be sent when a run ends (your computer offline, or the
server silent), it waits in `captures/reports/` in the tool's folder
and is sent the next time you run the tool. It is deleted after 30 days if
still unsent, and only the newest 99 are kept. To delete one before it is
sent, delete its file there.

## Readouts are separate

A [readout](readouts.md), which helps add support for a device, is made only
when you choose one; [its own page](readouts.md#what-is-in-one) says what it
holds. The short report of how a readout went, above, is not the
readout: it is sent as a conversion's report is, and turned off the same way.

## Providing your email address (optional)

As it exits, the tool asks whether you would like to receive a notification as more devices
are supported. You can
also sign up on the website's [notify page](https://firmwareliberator.com/notify).

You are sent an email with a link to confirm your email address. An
address never confirmed is deleted after 7 days. Once confirmed, it is used only for notification emails. [Privacy](privacy.md) says who keeps it, and your rights.

**Unsubscribing**  
Once you remove your address,
or if you never confirmed it, nothing more is kept or sent to it; to sign up
again, use the [notify page](https://firmwareliberator.com/notify).

