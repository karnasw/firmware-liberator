# Changelog

The conversion tool's own versions (`x.y.z`), not any firmware's. A patch
release fixes something; a minor one adds something; a major one is for a
change that stops a run started on an earlier version from being resumed, or
removes an option.

The version is set in one place, `tools/autoflash/__init__.py`. Merging a new
one to `main` tags it and publishes a release with its notes from this file, so add
the section below before changing the number. The release waits for the unit
tests, the final firmware on the emulator, the lint and the leak scan to pass
on that commit, and carries the firmware files the tool installs with
`SHA256SUMS`; GitHub signs an attestation of the release, its tag and files,
which `gh release verify vX.Y.Z` and `gh release verify-asset vX.Y.Z FILE`
check.

## 3.0.0

A major release because an option has gone: the tool's options say "device",
as its screens do, since it converts much more than smart plugs now.

- `--plug-ip` is `--device-ip`, and the hidden `--plug` is gone (`--run` names
  a run). A command or script that gives either needs the new name.

- A device can go back to Sonoff's firmware with no backup on your computer. On the
  way to Tasmota or ESPHome the tool keeps the device's Sonoff identity - its eWeLink
  id and key, model and kind, never its Wi-Fi - in the last 4 KB of its memory, an
  area of its own in the Tasmota layout, whose filesystem is 4 KB shorter for it.
  Leaving Tasmota, the identity goes into the settings of the next firmware. Going
  back without a backup, the tool fetches Sonoff's official firmware from eWeLink
  (or the public copy FlashDumps keeps) and writes it with the kept identity, or one
  rebuilt from your eWeLink account, and the Wi-Fi the device is on now; the run and
  its report say which. The backup comes first where it is there. Devices converted
  before keep Tasmota's own table, and go back from the account. Tasmota's
  Partition Wizard's "Resize FS to max" takes that area back: leave it alone.
- The tool converts devices with a classic ESP32 and other partition layouts, not
  only an ESP32-C3 on the S60's: the Sonoff POW Elite, POWCT, TH Elite and
  DUALR3, to Tasmota and to ESPHome, with their firmware pinned. A device whose partition
  table the tool does not know stops after its backup, before anything of
  Sonoff's is written over. The NSPanel, MINIR4, BASICR4 and M5 1C, 2C and 3C
  are not converted yet, so the tool turns them down as it
  does a device it does not know: `devices/` says what holds each
  one up, and `docs/adding-a-device.md` how to add a device.
- LAN Control no longer needs turning on in the eWeLink app, for a conversion or a
  readout. Every device is found on your network by the hardware address your
  eWeLink account gives it, and asked what it runs through eWeLink, as a Beken one
  already was, so Sonoff's firmware need answer nothing on the network. The Wi-Fi
  signal read before anything is written is the one the device last reported to
  eWeLink. With eWeLink out of reach, a run from Sonoff's firmware stops with
  nothing written, rather than listing whatever answered on the network.
- A device the tool does not convert yet can be read out to help add it:
  offered when the eWeLink account holds no supported device, or run for any
  device with `readout`, or for every device in the account at once (`readout
  --all`), each sent on its own as it is made, so the website can turn down one
  it has enough readouts of - those the tool converts too, as one may run new
  firmware. Before reading one, the tool asks the website whether it would add
  anything, naming the device as a report does (a hash of its MAC, never the
  MAC), so the website counts how many devices people would read out, not how
  many times they asked: the website keeps three readouts of each model and firmware
  version, each from a different person, and 20 a day from one place, so one
  like them, or a second of a model from the same place, is neither read nor
  worked for,
  and a file of the firmware eWeLink offers that it holds already is not sent.
  Its first stage only reads: what the device and
  eWeLink say it is, and the firmware eWeLink offers it; nothing is sent to the
  device, and a device the tool has not seen gets that alone - as does one the
  account still lists but that runs Tasmota, ESPHome or OpenBeken now,
  converted by the tool or by hand: the tool says what it runs. A readout's second stage
  reads the whole memory, and is only for a device the tool lists as Supported,
  Testers wanted or Readout wanted, under that kind's own firmware name. On an
  ESP32-C3 or classic ESP32 that is through the bridge, kept in the spare slot only
  until the device is switched off and on. On a BK7238 (which has one app area) it
  installs a pin-free OpenBeken for a few minutes, reads the flash, and puts the
  device's own Sonoff firmware back from the file eWeLink offers it - the whole
  round trip proven against the emulated S61. A readout of a whole memory logs
  what Sonoff's firmware says of the hardware it drives: meter and sensor chips, a
  temperature watch, an ADC, external RAM, Matter, its SDK. With the owner's agreement, a copy, with every Wi-Fi password
  taken out and its eWeLink identity made up, goes to a private store behind
  api.firmwareliberator.com; the firmware eWeLink offers the device goes with
  it, as a second version to test. See `docs/readouts.md`.
- As it exits, the tool asks once whether you would like an email as more
  devices are supported, offering your eWeLink account's address; the
  website's notify page asks the same. The tool's sign-up names the models of
  the devices in your eWeLink account and the readouts you sent, so you also
  hear of your own devices. Nothing is kept until you confirm from an email
  (sent again once, the next day), and every email has a link to remove your
  address. The tool keeps the address so as not to ask again; `email` changes
  it, or has your computer forget it.
- A run logs what a backup says about the device - its chip, secure boot and
  flash encryption, flash size, partition table and each app's version - and
  shows it where the table is not one the tool knows: none of it is secret, and
  one report is then enough to start adding that device.
- A run says a full disk plainly. Where a device that has downloaded from this
  computer then asks for nothing, it no longer blames this computer's firewall,
  and says to switch the device off and on. A Ctrl-C just before the first stage
  is no longer forgotten, so a second press within 10 seconds stops the run.
- The S61's backup is tried three times before the run goes on without one.
- On Windows the tool starts where PowerShell is set to run no scripts - even
  by a policy for the whole computer, which stopped it with "running scripts is
  disabled on this system": `start.cmd` and the README's line run its start
  script as text, not as a script file.
- A device that loses the upgrade command - a power cut part way through its
  download, say - is sent it again as soon as it is back on eWeLink, up to three
  times, rather than the run stopping about three minutes later for you to start again.
- On an S61 whose bootloader refused ESPHome three times, the run restarts it
  and sends ESPHome again, as it was meant to: the restart was not seen, as the
  device had only just restarted, refusing it, and the run stopped asking you to
  switch it off and on.
- On an S61, a firmware delivery a power cut stopped part way is said plainly -
  the device keeps its Sonoff firmware: switch it off and on, and run again - not
  as something unexpected.
- An eWeLink account made with a phone number can be used: give the number with
  its country code (+44 7700 900123) where the tool asks for the account's email,
  or as `--email` or `EWELINK_EMAIL`. One given in advance that is neither
  stops the run at once.
- A run signs in to eWeLink once, where it signed in again each time it asked
  eWeLink something (twice every 10 s while waiting for a device to come back),
  and in the region the account was last found in. The helper tools it starts use
  that sign-in too, so converting an ESP32 device signs in once, not four times.
- A device offline in eWeLink when a run sends to it is waited for five and a half
  minutes, not three: after a send cut off part way it stays away about five,
  and the run gave up just before it came back.
- A run stopped with Ctrl-C while it sent a device its firmware through eWeLink
  says to wait about 3 minutes before running again: the device stays away from
  eWeLink a few minutes after that, and a run started at once did not find it.
- The Sonoff Basic Gen5 (BASIC-1GS). The tool checks the partition table in
  eWeLink's official update file before writing anything, on every Beken device,
  and logs what the run learns. Beken devices on any other table are refused.
- On a Beken device, Sonoff's firmware - what a run checks before writing, keeps
  beside the backup and puts back - and the firmware name its updater checks
  come first from the file eWeLink offers the device, as on an Espressif one,
  and from a public copy only where eWeLink offers none the tool can use. Going
  back takes the copy kept beside the device's backup before either, as an
  Espressif device's own backup comes first. Either family's public copy is
  Sonoff's update file as FlashDumps publishes it, pinned by hash, for every
  device; an Espressif device took the newest listed, unpinned, and an S61 one
  made from a device's dump. Going
  back to Sonoff's firmware signs in to your eWeLink account for this if the run
  has not yet.
- A readout is safe on the Sonoff BASICR4 and MINIR4, whose firmware is a
  newer Sonoff SDK. The BASICR4 keeps its partition table at 0xC000, where the
  bridge did not look: bridge 4.4.6 takes it from there without writing
  anything, and then only reads, where an earlier one would have stopped and
  stayed on the device until it was opened. Both keep their eWeLink identity and Wi-Fi in
  records of their own, which a readout now takes out too: before, one would
  have sent in the device's identity and perhaps its Wi-Fi password. In the
  emulator both now start as a paired device does and are read out. The
  BASICR4 is not converted yet: Tasmota and ESPHome look for the table at 0x8000.
- Each device has one status, kept in `devices/status.json` and shown by a badge
  in the README and on the device's page: Supported, Testers wanted, Readout
  wanted or Not supported yet; every device's folder is in `devices/`, whatever
  its status. On a device wanting testers, the go-ahead before a run's first write says
  so and asks you to report your result, and the report asks for the run's log.
- The firmware this project builds says in its version which upstream release
  it is built on: OpenBeken 1.18.320-1 and ESPHome 2026.10.0-2. A device on a
  build they replace, such as the S60's ESPHome 1.6.0, is offered it as an
  update.
- This project's ESPHome and the mover call themselves `liberator.sonoff-s60`,
  `liberator.mover` and so on, as ESPHome and Home Assistant show them, rather
  than after a GitHub account. A device on an earlier image, named
  `adamsthws.sonoff-s60`, is still this project's ESPHome to the tool, and is
  offered the update. The README's badge shows OpenBeken's release, read from
  `artifacts/firmware.json`.
- A device whose Sonoff firmware ran from the second half of its memory can go back to
  it. The run used to stop before writing it, as Tasmota ran from there.
- A TH Elite or Dual R3 on Tasmota can go to ESPHome or back to Sonoff's firmware,
  through a temporary layout table.
- Every device ends with ESPHome's bootloader, however it got Tasmota: a device on
  Tasmota with another one goes through the full Tasmota that installs it, even to
  ESPHome. On a classic ESP32 the run used to stop for lack of memory, writing
  nothing.
- The installer can restart the device into its Sonoff firmware (`POST /restart`),
  just as switching it off and on would, so a run can put a device it left on the
  installer back without asking you to. It refuses while an upload is under way,
  and where an upload cut off part way has left no Sonoff firmware to start.
- The installer is now 4.4.6, with a read-only `GET /info`, and each run logs its
  answer at the backup step: the chip's exact revision, where a classic ESP32's
  log only said "at least", the flash chip's id and real size, the slots, whether
  PSRAM is in the chip's package, and the input pins' levels. An older installer
  only leaves these lines out.
- The installer's page no longer says Sonoff's firmware is chosen for the device's next
  start once an upload of Tasmota into its slot has been cut off part way, and its
  status says there is none from that moment: the device then starts the installer
  again when switched off and on, so you can carry on. Both said so only after a
  restart.
- A TH Elite on Tasmota no longer reports that its relay did not switch at the end
  of a run: the check did not read the answer Tasmota gives where a template has
  more than one relay.
- A device back on Sonoff's firmware no longer reports its relay check as failed
  while it is reaching eWeLink: the check waits, up to three minutes, for eWeLink to
  show the device online and for it to answer there, then switches the relay. An
  Espressif device's run checked straight away, seconds after its firmware first
  answered on the network and before it had reached eWeLink. And a device that drops
  off eWeLink as it switches has its relay put back as it was once it returns,
  rather than left switched over. A device asking eWeLink for its settings while the
  check waits no longer ends the run with "something unexpected went wrong".
- A device back on Sonoff's firmware has its meter read at the end of the run, as
  the eWeLink app reads it, after each switch of its relay: the report gives its
  readings with the relay on and off, and whether they show the relay switching,
  as on Tasmota and ESPHome. A device that gives eWeLink no readings, a Dual R3,
  has its relay checked alone as before.
- The end-of-run check always shows its Relay and Power meter answers. A device
  that stopped answering it on Tasmota or ESPHome, or a Dual R3 driving a
  motor, showed neither, or only one.
- Going back to Sonoff's firmware ends the same way on either chip family: the
  tool waits for the device on your Wi-Fi and online in eWeLink. One on your
  Wi-Fi that never reaches eWeLink is said to be kept off the internet, most
  likely by a firewall or a guest-network or parental-control rule. An
  Espressif device was looked for on the Wi-Fi alone, a Beken one in eWeLink
  alone.
- An Espressif device that has not come back on ESPHome or Sonoff's firmware
  has you switch it off and on, then is looked for again, as a Beken device
  is; the run stopped. The report says whether it had to be, on either family.
- A Beken device whose ESPHome cannot join your Wi-Fi is given it through
  ESPHome's own network, as an Espressif device is.
- A device whose new firmware cannot join your Wi-Fi - ESPHome, or the mover
  on the way to it - is set up through its own network as soon as that network
  is up, a minute or two in, where the run waited three minutes first.
- The end-of-run check of the relay tries a switch, or a reading of the
  meter, again when its answer is lost: a device whose Wi-Fi blinked as it
  was checked - ESPHome taking its own network down once back on yours - was
  reported as a relay that did not switch, its meter showing it had.
- A Beken run carried on after one cut off while OpenBeken fetched its next
  firmware restarts OpenBeken before asking it to fetch: the fetch the last
  run began could still be waiting inside it, and OpenBeken turned the new one
  down while saying it had started it, so the run waited half a minute first.
- A command to OpenBeken that it answers "No command entered" - its web
  server reads only what has arrived as a request starts, so the words can
  miss it - is sent again; the run stopped there.
- A Beken device that had to be restarted before it fetched ESPHome is no
  longer taken for one that refused the image: the run sent ESPHome again into
  a device restarting to install it, and could end stopped there.
- A Beken device that loses power or Wi-Fi while OpenBeken fetches its next
  firmware - ESPHome, or Sonoff's back - stops the run with what to do (switch
  it off and on, then run the same command again), as a cut-off send from
  Sonoff's firmware does: it said "something unexpected went wrong" with an
  error behind it. A readout cut off the same way - sending its firmware,
  reading the device's memory or putting Sonoff's firmware back - says what
  to do too, where it was recorded as having crashed.
- A device that raised its own network while your router restarted, then
  rejoined your Wi-Fi by itself once the router was back, is carried on with:
  the run had you join the device's network regardless, and stopped after 15
  minutes unattended. Waiting for you to join it, the run now looks for the
  device on your Wi-Fi too, on either family.
- A device whose Wi-Fi drops for a moment as it downloads its new firmware no
  longer leaves a Python traceback ("Exception occurred during processing of
  request", ConnectionResetError) across the run's screen: the dropped request
  goes in the run's log, and the device asks again for what it lacks.
- The report sent says whether your computer joined a network the device
  raised itself on either family, and now also when ESPHome or the mover was
  set up through its own network; it said so only for an Espressif device's
  installer or Tasmota. Each stage in it also says whether that was needed
  there and why - the device never given your Wi-Fi details, given them but
  not joining, or its settings lost - and how many times the device was
  switched off and on during it, so a stage that keeps needing either shows.
- A device that turns the first firmware down while an update cut off part way
  still holds it (eWeLink's error 410), or that has left eWeLink since, has you
  switch it off and on, then is sent it again once eWeLink shows it online, on
  either chip family; the run stopped. A readout of a Beken device does the
  same.
- `--dry-run` writes nothing on either chip family: it goes through the steps
  that only read and stops before the first that writes, and `step <id>
  --dry-run` does only a step that reads. On an Espressif device it went on to
  send the installer, replacing Sonoff's firmware.
- The end-of-run check also asks the device which release of Tasmota, ESPHome or
  OpenBeken it runs, and reads a TH Elite's temperature probe, if one is plugged
  in: the report's Checks fails if the release is not the one this version
  installs, as this project built it, or a reading is one the sensor cannot give.
  The report sent says the same: a device that did not answer the check is
  "failed" there too, as on the screen.
- A device on Tasmota, ESPHome or OpenBeken that loses power or its Wi-Fi as a run
  ends is waited for, up to four minutes, before its relay and meter are checked,
  rather than reported as not answering the check.
- An ESPHome update is said done only once the device has been seen running it for a
  whole minute on end, the minute it takes to keep it. One that stopped answering
  part way, switched off say, was said done all the same; it is now waited for,
  and its minute starts again once it answers.
- An ESPHome update cut off part way - this computer crashing, say - and sent
  again at once is waited for, about two minutes, while the device still waits
  for the rest of the first, then sent again: every try in that time timed out,
  and the run stopped.
- A stage that passes leaves just its line on the screen. What it showed while
  under way stayed above it: a try that failed on the network and was tried
  again, drawn as a stage stopped and then run a second time; instructions to
  join a network, long since done with; the installer sent again. A step done
  again because a later one found it had not taken is drawn afresh too, and the
  run says why only if it still does not take. While a stage is done again its
  line says "Retrying" rather than "Underway". Instructions to join a network go
  as soon as it is joined, and a stage's line is no longer drawn again under each
  of its notes, so a stage with much to say stays short enough to clear. A window
  under 40 rows is told, in a line, that maximising it fits the report at the end
  on the screen; one of 30 rows or fewer is waited for to grow.
  The run's log keeps everything.
- A conversion an earlier version began, carried on by this one, ends on the
  release of Tasmota, ESPHome or OpenBeken this version installs. Where the earlier
  version had already installed its own release - official Tasmota, from the point
  the conversion must finish on it - the run finishes, then goes on to update it in
  the same run, its settings kept. It was left on the earlier release. A run
  stopped part way through such an update finishes it when run again.
- Every question says how to quit the same way: "(q to quit)", in grey, after it.
  Menus no longer number a Quit option, and q quits at every one. After a Ctrl-C,
  the run carries on by itself, or at once with Enter; q stops it there.
- The report at the end is half as tall: two columns on the screen - the device on
  the right, the result, the run and the computer on the left - with shorter labels (Model,
  Backup, Checks, OS, Liberator for the tool's version...) and answers (Python's
  "(venv)", the checks "Pass" or "Fail"), and no heading of its own, nor a
  Result: the line above it says how the run ended ("Conversion complete!",
  "FAILED: ..."), as a readout's now does for each way one ends, and a
  readout's report lists each device under how it ended. From gives Sonoff's firmware with its version, "Sonoff 1.2.0", in
  place of a line of its own. Whether a power meter's calibration carried over
  is no longer said on the screen, where it worried more than it helped, nor
  where going back to Sonoff's firmware took the device's eWeLink identity from,
  nor whether the Wi-Fi was typed in, a setup network joined, or a Beken device
  switched off and on. The run's time, each stage's and how long it kept the device busy are no
  longer shown: the report the tool sends holds them, and the meter's. A stopped run's result says
  "Stopped", where and why being said as it stopped, and in the report copied for
  an issue. A run stopped before any stage
  began no longer asks for a report.
- A device the tool converted is asked which firmware it is to have where one on
  Sonoff's firmware is: at "Choosing the firmware", once the list of what has
  to be in place has been read - not before the first stage, ahead of it. An
  update says so on its option alone ("ESPHome, update to 2026.10.0-2"), with no
  line above the options on the release the device runs.
- Sonoff's firmware is offered to a converted device with no backup on your computer
  too, as almost every one can go back without it: its identity is the one it
  keeps, or comes from your eWeLink account. It used to be offered only to a device
  on Tasmota keeping its identity, and the rest needed `--firmware sonoff`. If
  Sonoff's firmware cannot be put together after all - no identity anywhere, or
  its download failing - the device goes back to the firmware it came from, ESPHome
  or Tasmota, instead of being left part way, and the report says "Put back on".
- A yes/no question is taken off the screen once answered, even after a wrong
  answer first. It stayed, under "Please answer y or n.", when nothing was
  shown above it.
- A conversion or a readout that finished asks what next: convert another
  device, or read one out. With several devices, you go from one to the next
  without starting the tool again or signing in to eWeLink again.
- A run stopped before it wrote anything to its device - at a question, say - is
  no longer offered back as "A conversion did not finish last time": starting
  afresh loses nothing.
- A conversion run with `--wifi` no longer asks for the Wi-Fi details again near
  its end, as it installs official Tasmota's safeboot: it uses those typed earlier
  in the same run.
- Two Wi-Fi passwords typed that differ are taken off the screen before the
  password is asked again, as any wrong answer is; they stayed under each other.
- The arrow keys move along a typed answer on Linux and macOS. The terminal used
  to echo them: the up arrow moved the cursor into the rows above, garbling the
  screen, and their codes became part of the answer.
- On Windows, a key pressed by accident at a question no longer changes the
  answer: the console's up arrow or F3 brought the last answer back, so a
  stray one could answer the consent question with an earlier PROCEED, F7
  opened a list of earlier answers over the screen, and Esc cleared what was
  typed. The arrows, Home, End and Delete still edit the answer.
- F2 no longer stops the countdown after a Ctrl-C: in many terminals its code
  ends in a Q, which was taken as q.
- A Ctrl-C pressed before the first stage, while the tool looks for devices, no
  longer leaves "! Are you sure?" on the screen for the rest of the run.
- A Ctrl-C pressed while nothing is being asked is asked about before the next
  question, not after it: pressed while the tool looked for devices, "You
  pressed Ctrl-C" came only once a device was chosen and a key pressed, at the
  end of the first stage.
- A stage that shows notes keeps one line: its spinner turns on the line above
  them. It was drawn again, turning, under the last note, so the stage showed
  twice until it ended - unless the notes fill the window, where the line above
  is out of reach.
- The countdown after Ctrl-C counts down in an 80-column window too: its words fit
  one row ("Carrying on to the next stage in 20 s, or press Enter now"). On two,
  only the second was redrawn, and "in 20 s" stood still above it.
- A run stopped by the computer as a question waits - shut down, or sent
  SIGTERM - shows its stage stopped and takes the question off, as Ctrl-C does.
  It left the stage under way and the question waiting.
- Notes broken over several lines fit an 80-column window. Some were wider, so
  each line wrapped on its own and left a word or two on a row by itself.
- What the tool says on the screen is true where it was not. A readout of a
  Beken device stopped with Ctrl-C says to run the readout again, which puts
  Sonoff's firmware back: it said to switch the device off and on, which there
  starts the reading program again, as it stands in Sonoff's firmware's place.
  Quitting at a question says "Stopped as asked. Run this again to carry on."
  in place of "Nothing was left half done", untrue after a quit that leaves the
  device on the installer. A run turned away because another run holds the
  device says nothing was done once, not twice. Every go-ahead before the first
  write says "DO NOT CUT ITS POWER unless this tool tells you to", in the same
  words: "during the process" contradicted the later notes that ask for the
  device to be switched off and on.
- What the tool prints is shorter: most notes, questions' help and warnings
  take half the rows or fewer, and what explained rather than asked anything of
  you - why the startup firmware needs your Wi-Fi, what a check found, the
  hashes of a region that differs - is in the run's log alone. A stage is named
  by its number, never a step's code ("doing stage 9 again", not "Doing B2
  again"). Each thing has one name on the screen: the installer (its stage is
  "Sending installer firmware"), the startup firmware and the device's setup
  network; words such as bundle, pinned artifacts and app0 are gone. The
  readout's "Preparing to send" fits an 80-column window: it was cut short.
- A download the server turns away for now - GitHub's release downloads
  answering "504 Gateway Time-out" for minutes at a time, say - is tried again
  for a little over two minutes, waiting longer each time, rather than three
  times in ten seconds: the official Tasmota files, Sonoff's firmware for a Beken
  device, and the firmware eWeLink offers a device. A run stopped by one before had
  to be started again.
- A step whose device restarted just after fetching its instructions - switched off
  at that moment, say - is sent them again before its start. It was sent only the
  start, which a restarted device no longer knew ("'autoflash_run' undeclared"),
  until the step failed and was tried again from the top.
- The upload of Tasmota to Tasmota that most conversions make part way (D4), cut
  off by the network, is sent again only after a restart. Sent again at once, it
  went to a Tasmota still holding the upload cut off, which never took it: the
  run waited ten minutes and asked for it to be uploaded by hand.
- A device's boot is read only once Tasmota has counted it, 10 s after it starts.
  Read sooner, as just after a restart, the count went up by one later in the
  same boot, so an upload cut off could be taken for one the device had taken and
  started, and a restart could be seen where there was none.
- OpenBeken for the Sonoff S61 rebuilt with
  [1.18.320](https://github.com/openshwprojects/OpenBK7231T_App/releases/tag/1.18.320).
- A Sonoff S61 converted to OpenBeken says in the report the tool sends whether
  its meter's factory calibration carried over, as on ESPHome: it said it was
  not checked. OpenBeken's driver answers `AutoflashCalibration`,
  working out again what it takes from Sonoff's store and comparing it with
  OpenBeken's calibration, so it says it was not carried over once you calibrate
  the device yourself.
- ESPHome on the Sonoff S61 reads its voltage and current the right way round.
  Its meter, a BL0937, measures the voltage while its SEL pin is high, where
  ESPHome sets it high to measure the current, so each showed the other: about
  20 A whatever was plugged in, and a voltage that rose with the load (5 V at
  100 W). The power was right. SEL is now inverted in its config, as ESPHome's
  page says for this chip.
- Each run sends the project an anonymous report of how it went: the device,
  the firmware it went to and from, and how far it got - for a run you stop,
  the stage and how you stopped it, and for a conversion, how many devices
  this computer has converted; with what helps find a fault, such as where in
  the tool's code it went wrong, the device's chip, flash and firmware name,
  and Wi-Fi or Ethernet, a VPN or a firewall on your side - never anything of
  you, your computer or your network. Each device read out sends one too, as its
  readout ends: how it ended (accepted by the website, not needed, kept, and
  so on), never the readout itself. Reports from everyone add up to the figures
  on [firmwareliberator.com/stats](https://firmwareliberator.com/stats), which
  publishes only totals; the reports themselves are kept, so how devices fare can
  be followed over the years.
  `--no-report`, or `AUTOFLASH_NO_REPORT=1` in `.env`, stops it, and the tool
  says so once, the first time it runs on a computer;
  [what the tool sends](docs/what-the-tool-sends.md) says what a report holds.
  A report that could not be sent is kept on your computer and sent the next
  time, for up to 30 days.
- A run that failed ends with where to open an issue for it,
  firmwareliberator.com/issues, its report's ID under it, its report, the ID
  with it, on the clipboard to paste in, and where its log is, in place of the
  links to support the project; one you stop yourself does not. A readout that
  failed does the same under its report, which says "Failed". A run that worked
  no longer says where its log is, a device wanting testers' included, and
  leaves the clipboard alone. The end of a run no longer asks for its report to be posted in
  Discussions, nor links to them when a run fails.
- The names of what the tool keeps say "device" too. Each device's records
  are in `captures/devices/<MAC>/`, with `device.json`, where releases up to
  2.3.0 kept them in `captures/plugs/<MAC>/` with `plug.json`.
  The first time it starts, the tool renames that folder where it is - nothing
  in it is copied or rewritten, a Sonoff backup least of all - and it carries
  on a run an earlier release began. It stops, changing nothing, while a run
  of the earlier release is still converting a device, or if both folders
  exist. Its warnings say "do not cut its power" where they said "do not
  unplug it".
- In the emulators the same: `emu.sh --device MAC` and `--device-dir`, the
  `device off`, `on` and `cycle` commands in the Espressif emulator's
  terminal, and `beken-emu.sh device`. Both emulators' images are new.
- The `README.txt` saved beside an Espressif device's Sonoff backup is true of
  any of them. It told every owner their device was an S60: an ESP32-C3 in a
  welded case, to be written back with `esptool.py --chip esp32c3`. It now
  says the tool writes the backup back over the air, and gives the command
  that suits either chip.

## 2.3.0

- The tool's code is a package of modules, each for one thing - the command
  line, a run's context, its route, running it, its report, the plug in its
  eWeLink account - with each chip family's steps under
  `tools/autoflash/routes/<family>/`, where one file held it all before.
  Nothing a run does changes.
- The tool converts the Sonoff S61 (`--firmware openbeken`, `esphome` or
  `sonoff`), in every direction: from Sonoff's firmware through eWeLink, from
  OpenBeken or ESPHome to the other or back to Sonoff's firmware, each one
  update the plug installs as it restarts; the plug's own Wi-Fi and meter
  calibration stay on the plug for each firmware to read. Its backup (the
  plug's own data, and Sonoff's firmware from the public dump) is taken
  through OpenBeken's flash reads after the first conversion: a run to
  ESPHome installs OpenBeken first, as the interim firmware, backs the plug
  up through it, then has it fetch ESPHome, so ESPHome keeps no way to read
  the plug out. The tool sends the interim firmware with a marker after it,
  naming the app it installs, which is how the firmware knows it is the interim
  one; a marker for any other app never counts, and is erased. The interim
  firmware is named `autoflash-recovery-CDEF` from its first start, as the
  S60's interim firmwares are, and once it has fetched the next firmware it
  erases its own settings, so ESPHome - or a later OpenBeken - finds nothing
  of it. A run that lost its record recognises a plug left on it, and goes on
  from there. Tested end to end in the emulator in every direction.
- From Sonoff's firmware the run finds the plug in eWeLink before it asks
  which firmware you want, because which firmware a plug can have depends on
  what plug it is. The question is a stage of its own (Choosing the firmware),
  after Discovering the plug; with `--firmware` you are not asked.
- The S61's firmware is published: OpenBeken 1.1.0 (OpenBK7231T_App at
  `ce1abad` with the 4 MB table, a hook before an OTA's restart, the tool's
  OpenBeken driver and the S61's table for it) and ESPHome 1.2.0
  for the S61 (built with ESPHome
  [2026.10.0](https://github.com/esphome/esphome/releases/tag/2026.10.0) on
  LibreTiny 1.13.0), built and pinned by `tools/repo/update_firmware.py
  openbeken` and `esphome --plug sonoff-s61`.
- The repository is laid out by what things are: `tools/espressif/` and
  `tools/beken/` for each chip family's tooling and emulator, `tools/ewelink/`,
  `tools/repo/`, `devices/<plug>/` for what is one plug's alone, `tests/`
  mirroring `tools/`. The published firmware, `firmware.json` and `SHA256SUMS`
  are together in `artifacts/`, where `SHA256SUMS` names its files bare, so
  `sha256sum -c` works in the directory a release's files are downloaded to.
  On Windows, `start.cmd` is the one start file, whether you double-click it
  or run it from a terminal.
- **Breaking for Home Assistant adoption**: the ESPHome configs moved to
  `devices/sonoff-s60/esphome.yaml` and `devices/sonoff-s61/esphome.yaml`,
  and the `package_import_url` a plug gives Home Assistant names the new
  paths. A plug on ESPHome 1.4.0 or earlier names the old path, which no
  longer exists, so Home Assistant cannot adopt it until it is updated to the
  image built from the moved config - which running the tool again does
  (below); the plug itself runs as before.
- When Home Assistant adopts a plug, it now writes a short config that loads
  this project's ESPHome config, instead of a full copy. It uses the release
  your plug's firmware came from, which cannot change, so your plug gets a fix
  only when you choose a newer release. A plug adopted earlier keeps its copy;
  `docs/home-assistant-adoption.md` explains how to switch it to the short config.
- ESPHome for the S60 is 1.6.0: a change to its config, built with ESPHome
  [2026.10.0](https://github.com/esphome/esphome/releases/tag/2026.10.0) as
  1.4.0 was. Its bootloader is unchanged, so a plug on ESPHome already is
  not given a new one. It is the image built from the moved config, and
  names the plug as every firmware does (below). Its apparent power,
  reactive power and power factor are worked out each second from the
  calibrated voltage, current and power of the same readings, where they
  came from the meter's raw readings:
  a load close to resistive took those past a power factor of 1, and the log
  filled with each reading refused as impossible, many a second
  ([reported](https://github.com/adamsthws/firmware-liberator/discussions/1#discussioncomment-19373248)).
  Reactive power is 0 while power is within 1.5% of voltage times current:
  the meter gets them within about 1% on a resistive load, where a 1 kW
  heater used to read up to 148 var. And they change only in a second steady with
  the last: as a load switches, the meter's power follows its current a
  second or three behind, and a 2 kW heater's thermostat used to read up
  to 1782 var and a power factor of 0.66.
- Every firmware the tool installs names the plug alike, in lower case as
  ESPHome requires: the firmware and the last three bytes of its MAC, as its
  hostname and its name inside the firmware - `tasmota-abcdef` (Tasmota's
  device name too), `esphome-abcdef` (its friendly name `esphome abcdef`) and
  `openbeken-abcdef` (both of OpenBeken's names, its mDNS and DHCP
  hostnames), where OpenBeken was named `S61`. What plug it is goes in
  Tasmota's template, `Sonoff-S60` where it was the product model (`Sonoff
  S60TPG`), and ESPHome's comment: one for every model, so one image serves
  them all. The report gives neither the name nor the plug type and region:
  the model line says both.
- The S60's installer is sent to Sonoff's updater as version 9.9.9, as the
  S61's firmware is, where it was sent as its own version (4.4.4): a Sonoff
  release numbered above the installer would otherwise have stopped the
  conversion, should the S60's updater take only a newer version as the
  S61's does. The installer itself, and its published file, are unchanged.
- The report says whether the end-of-run checks passed (`Checks passed`),
  instead of giving the relay's and the power meter's results: what is checked
  can differ from one kind of device to another, and the report stays the
  same. For a plug the checks still switch its relay off and on, and what
  they found is in the run's log.
- A run from a converted plug - back to Sonoff's firmware, say - reports
  the plug's model and its Sonoff firmware from what the tool kept of the
  plug at its first conversion; it used to give `__` for both.
- While the report is put together after the last stage - the device's
  checks take a few seconds - "Generating your report..." shows with the
  stages' spinner, and goes when the report is shown.
- The links after a successful run start with GitHub Sponsors, before Ko-fi
  and PayPal.
- The addresses of a list of links line up under one another.
- At PROCEED, an answer other than the word - an Enter or a y pressed by
  habit - asks again ("Only the TYPED word PROCEED is accepted"); it used to
  stop the run. q stops. Ctrl-C there stops the run at once, as at every
  question; it used to count as a first press during work.
- A Ctrl-C at a question is not shown as `^C`, and what the run says next
  starts on a row of its own.
- A run starts at the top of the window, with all of it to use: what the
  window showed is scrolled up into its scrollback, not cleared, so it can
  still be scrolled back to.
- A question cut short by Ctrl-C or q - PROCEED, say - is taken off the
  screen with its note when the run stops, as an answered one is; it used to
  stay, with its stage shown twice: underway above it, stopped below.
- If a step was interrupted last time and the device does not show it
  finished, the tool now repeats it by itself. It used to ask you, and if you
  answered "it did finish" for a step that had not, the run waited for good on
  a firmware never sent.
- Never two blank lines in a row: one left above something taken off the
  screen - "Looking for supported devices..." - no longer doubles the next.
- In VS Code's terminal a run starts under the command that started it,
  where it starts at the top of the window elsewhere: VS Code pins a command
  scrolled out of sight over the window's top rows, which covered the run's
  first.
- An S61 whose new firmware its bootloader would not install starts
  Sonoff's firmware again: the run now sends the firmware again by itself
  (twice at most), instead of asking you to switch the device off and on again
  and again. You are asked PROCEED once a run, not again for this. A run that
  stopped to ask you to switch the device off and on takes it as done when you
  run it again. The same goes for ESPHome fetched by OpenBeken (J2, O1), which
  restarts by itself to install it: if OpenBeken's uptime shows it has
  restarted since, ESPHome was not installed, and it is fetched again, with no
  switch off and on to ask you for. The S61's fault suite has each case, an
  image its bootloader refuses (`--simulate-bad-image STEP`).
- A step recorded as done that the device disagrees with is now done again by
  itself; the run used to ask you whether to: on an S61, a plug still on the
  firmware it was leaving; on an S60, a high slot, partition table or
  Safeboot not as the step left them, or the recovery firmware where the
  official app should be.
- A device not seen on the network at the start is looked for twice more
  by itself, 15 s apart, before the run asks you what to do: one just plugged
  in, or on a busy Wi-Fi, answers a moment later.
- If the device gives no answer to a password or key check, the tool tries
  again by itself with what you typed, before asking you to choose again.
- On an S60, an upload cut off by the network is sent again by itself once
  the device shows it did not take it; it used to be left to you to upload
  through the device's page. An upload the device took, with only its answer
  lost, is not sent again.
- More steps are tried again by themselves after a network blip: ESPHome's
  arrival check and Tasmota put back on an S60, and the interim firmware
  made the plug's own OpenBeken on an S61.
- On an S60, a backup whose two reads fail is read twice more by itself
  before the run asks you whether to try again or carry on without one.
- A blank line stands between every question and the options or text above
  it.
- A first Ctrl-C asks "! Are you sure?" under the stage line; it used to show
  after a second copy of that line. It counts down the seconds in which a
  second Ctrl-C stops the run at once, and goes when they are up.
- If your plug is on an earlier release of this project's ESPHome, the tool
  offers this release as an update (`--firmware esphome` chooses it in
  advance). It sends an ordinary ESPHome update, keeps the plug's settings and
  reads the Wi-Fi from the plug, so a fix reaches a plug no ESPHome dashboard
  builds for. The tool offers it only to an ESPHome that says it is this
  project's for its kind, through its API, and whose API is not encrypted: you
  update a plug adopted into a dashboard from that dashboard.
- The same for a Sonoff S61 on an earlier release of this project's
  OpenBeken (`--firmware openbeken`): it fetches this release as any OpenBeken
  update, its settings kept. Only an OpenBeken whose driver says it is on
  an S61, and whose build says it is an earlier release, is offered it.
  Upstream's own builds have neither the driver nor the S61's 4 MB layout,
  so this is how you update an OpenBeken this tool installed.
- And for official Tasmota older than the release the tool installs
  (`--firmware tasmota`): the tool switches it to its safeboot and gives it
  that release there, as the conversion does, keeps its settings and template,
  then checks its meter. Only an official release build is offered it, never
  anyone's own. Tasmota's own update, from its page, works as ever; with this,
  running the tool again updates any plug it converted.
- A device hanging up on the tool as it writes - a plug restarting under a
  check - no longer ends the run on the spot, with nothing cleaned up: the
  tool set SIGPIPE to end it, for quiet output into `head`.
- A plug given by address that does not answer yet, and that this computer
  converted, is waited for as it restarts, as one found by searching already
  was. It was taken for a plug on Sonoff's firmware, and the run went on to ask
  eWeLink for it.
- `status` says when a plug has an earlier release of its firmware than this
  version installs.
- A plug adopted into an ESPHome dashboard is left to that dashboard even if its
  API has no key: the tool offers an update only to a build this project
  published, by when it says it was built, so an update cannot overwrite the
  owner's config.
- A plug on ESPHome whose relay its owner renamed is known by the kind its
  config names. It was taken for one on Sonoff's firmware, and the run stopped
  waiting for eWeLink.
- `--replace-own-build` installs this project's ESPHome over a build made
  elsewhere, such as a plug adopted into an ESPHome builder, for an owner who
  wants to stop using that build. It says what is lost and asks first.

## 2.2.0

- ESPHome for the S60 is 1.4.0: a change to its config, built with ESPHome
  [2026.10.0](https://github.com/esphome/esphome/releases/tag/2026.10.0) as
  1.3.0 was. It corrects the meter as Sonoff's firmware does: by the 1.02
  its board calls for, and by the plug's factory calibration
  ([#7](https://github.com/adamsthws/firmware-liberator/pull/7)) - from Sonoff's
  settings where the plug still has them, or, on a plug converted through
  Tasmota, which lost them, from its Sonoff backup, which the tool now
  gives it. Energy, and the daily, monthly, yearly and total energy, are
  counted from the meter's own pulses, calibrated the same way, so they
  agree, and stay right at loads too small for the power reading; the
  totals carry on from 1.3.0's. Its bootloader is unchanged, so a plug on
  ESPHome already is not given a new one.
- A plug converted to Tasmota has its meter calibrated as Sonoff's firmware
  had it: by its board's factor, which read 2% low on Tasmota before, and
  by its factory calibration from its Sonoff backup. Tasmota takes one
  scale for each reading, so the factory calibration's set from 300 W up
  does not carry over. A plug converted before is calibrated by running
  the tool on it and keeping Tasmota (`--firmware tasmota`); one calibrated
  by hand is left as it is.

## 2.1.3

- A plug converted to Tasmota went on to switch nothing after Tasmota had
  restarted six times in a row, each within 10 seconds of starting: taking
  that for a boot loop, Tasmota goes over to its fallback module, Generic,
  keeping only its Wi-Fi ([#8](https://github.com/adamsthws/firmware-liberator/discussions/8)).
  The plug's template is now its fallback too. The final check (H1) makes it
  so on a plug converted by an earlier version, and puts the template back
  on one that has already gone over to Generic.
- A plug on ESPHome in safe mode - which ESPHome starts after it was stopped
  within a minute of starting ten times in a row - was taken for one part way
  through a conversion: to stay on ESPHome, the run sent it the startup
  firmware and installed ESPHome again. It is now known for ESPHome in safe
  mode, and the run waits the five minutes until ESPHome restarts as itself.

2.1.2 was withdrawn on the day it was published, from a commit holding work
not meant for release; its number cannot be used again. 2.1.3 is the tool
2.1.2 was.

## 2.1.1

- A plug converted to Tasmota could finish without the plug's template -
  when it did not go in with Tasmota's reset and the second try did not take
  either - and so switch nothing: Home Assistant showed only its restart
  button and Wi-Fi ([#8](https://github.com/adamsthws/firmware-liberator/discussions/8)).
  The run now checks the template is in place before it calls the plug
  converted, and puts it back if not. The final check (H1) does the same, so
  running it on a finished plug puts a missing template back, keeping the
  plug's other settings.
- A converted plug that had lost its template was taken for one part way
  through a conversion, and sent an update only ESPHome takes: the run
  waited 4 minutes, and stopped. It is now known for a Tasmota without its
  template - found on the network, or given with `--plug-ip` - and goes on as
  any converted plug does; choosing Tasmota gives it the template alone,
  keeping its other settings, such as MQTT.
- A device with ESPHome's update service and no web page was listed as a
  plug part way through a conversion, whatever it was - another ESPHome
  device on the network, say. Only a plug this computer has a record of is
  listed so now.
- A run stopped before it wrote anything no longer hides, from the next run,
  the run before it that knew where the plug had got to.
- The go-ahead to replace a plug's Tasmota or ESPHome is asked once a run,
  not again when a step is tried again by itself.
- The report of a run from a converted plug said "Sonoff backup: Not taken"
  although this computer had the plug's backup; it now says it was taken by
  an earlier run.
- On Python 3.10, a terminal that reports no size - ssh without one, some
  IDE consoles - was taken as 20 columns wide, and every line wrapped or cut.

## 2.1.0

- A converted plug ends on Tasmota
  [16.0.0](https://github.com/arendst/Tasmota/releases/tag/v16.0.0), the
  latest release, where it ended on 15.6.0. Its files now come from the
  release's own page on GitHub. A run left part way by an earlier version
  carries on by itself: with 16.0.0, or - if it had installed 15.6.0 already -
  on 15.6.0, checked byte for byte as ever, which Tasmota's own page updates.
- ESPHome for the S60 is 1.3.0: the same config, built with ESPHome
  [2026.10.0](https://github.com/esphome/esphome/releases/tag/2026.10.0),
  the latest release, where 1.2.0 was built with 2026.9.1. Its bootloader is
  unchanged, so a plug on ESPHome already is not given a new one.
- The firmware the tool installs is moved to each new Tasmota and ESPHome
  release one way, written down in
  [docs/updating-firmware.md](docs/updating-firmware.md).

## 2.0.2

- The steps that write firmware keep the plug busy for about 40% less time:
  18 seconds where they took 30, on the emulator. Every block written was
  read back and sent to this computer to compare, doubling what crossed the
  network while the plug could answer nothing else. Each block now comes
  with its hash, checked on the plug before it is written - a block damaged
  on its way is asked for again, alone - and each step's report still
  hashes everything it wrote, which this computer checks.
- A step's 20 seconds of retries on the plug were counted from its first
  failure and never begun again, so after one short Wi-Fi drop a later one
  found them spent, and the step stopped ("returned HTTP -1 after 1
  attempts"). They now count only the time spent failing
  ([#5](https://github.com/adamsthws/firmware-liberator/pull/5)).
- The plug or the network dropping a request part way through a step - a
  connection reset in a Wi-Fi outage, say - was put as "something unexpected
  went wrong", though the step was tried again as for any other network
  trouble. It now says the connection to the plug was lost.
- A plug the run had just restarted onto your Wi-Fi could be taken, as it
  started up, for one that had lost its settings, and the run asked you to
  join a network of the plug's that had gone a moment later - then, after 15
  minutes, stopped with "something unexpected went wrong". The plug is now
  given a minute to come back first, and a run that gives up on its network
  stops at that step, as any other.
- A send of the installer that failed before the plug was told to fetch it -
  eWeLink's servers dropping this computer's connection, say - was put down to
  this computer's firewall keeping the plug out. It now says the connection to
  eWeLink failed and nothing was sent, or, for a send refused, only that
  nothing was sent.
- Swapping the temporary startup firmware for the official one (G1) first
  checks the plug runs official Tasmota; a Wi-Fi blip as it asked stopped the
  step, as if the check had failed. The plug is now asked again once it
  answers.
- A Tasmota plug that did not answer for a moment when the run read its
  Wi-Fi details - a Wi-Fi blip, say - had you asked to type them. It is now
  asked again once it answers, before you are.
- The command that starts a step on the plug, lost to a Wi-Fi blip before it
  reached the plug, left the step waiting for a plug that never began, then
  failing. It is now sent again once the plug answers.

## 2.0.1

- A plug brought back from Tasmota's own safeboot finished its conversion,
  then the run stopped with "something unexpected went wrong" instead of its
  report, which could not name where the plug started. A Tasmota there with
  a web password is now asked for it, where the run stopped, and the rest of
  the run treats the plug as the Tasmota it was.
- The look round the network for a converted plug, 5 to 10 seconds - longer
  when one this computer converted may be restarting - showed nothing on
  screen, so the run seemed to wait for a key; one pressed then was taken as
  the next question's answer, an Enter as its default. The look now says so
  while it looks, then leaves the screen, and a key pressed while nothing is
  asking is dropped.
- The firmware question gives the firmwares by name alone.

## 2.0.0

ESPHome, as well as Tasmota: the run asks at the start which the plug should
end up running. And a plug can move between them later, or back to Sonoff's
own firmware.

A major release because the tool is now Autoflash, and the names that called
it after the S60 have gone:

- `tools/s60_autoflash.py` is `tools/autoflash_cli.py`. `start.sh`,
  `start.ps1` and `start.cmd` run it as before; a command that named the
  old file needs the new name.
- The settings it reads start `AUTOFLASH_`. In `.env`, `S60_WIFI_SSID`,
  `S60_WIFI_PASSWORD` and `S60_SUDO_PASSWORD` are still read, and the run asks
  for them to be renamed; `S60_PYTHON_HOME` is not, and is now
  `AUTOFLASH_PYTHON_HOME`.

- A run looks for the plug first. One already on Tasmota or ESPHome is offered
  the other two: Tasmota, ESPHome, or Sonoff's own firmware and settings put
  back from the backup taken when it was first converted (only where that
  backup is on this computer). One left part way through a conversion is
  recognised, and can go on to any of the three.
- Every way round is over the air and checked as the first conversion is:
  every block read back and compared byte for byte, and the plug switched
  only once all of it is.
- The bootloader becomes ESPHome's on every route, once: it starts all three
  firmwares, and rolls back an update that fails to start. The final check of
  a Tasmota plug now covers it too.

- Choosing ESPHome needs nothing built or installed: the run installs a
  ready-made ESPHome for the S60 (`ESPHome/sonoff_s60.yaml`), with the plug's Wi-Fi
  written in, in memory only. It joins your Wi-Fi by itself, has no password
  to start with, as Tasmota has none, and Home Assistant finds it and can
  adopt it.
- Every block of ESPHome is read back and checked before the plug switches to
  it. The plug's memory layout stays as Sonoff made it; its bootloader becomes
  ESPHome's own, so ESPHome can roll back an update that fails to start.
- The answer is kept with the run, so carrying on an unfinished run carries
  on the same way. `--firmware tasmota` or `--firmware esphome` answers it in
  advance, as an unattended run must.
- The report says which firmware the plug ended up with, and the relay and
  meter are checked through ESPHome on its route.
- The report calls Windows 11 so where Python 3.10 or 3.11, not the tool's
  portable one, called it Windows 10.
- The report's answers are shorter: "(portable)" for the tool's own Python,
  "Failed" for a backup that failed, "Switches (meter confirms)" for the relay.
- The plug's own network is looked for by this plug's names only - Tasmota's,
  and the `ESP_` name it sometimes shows instead, which was missed before - so
  a neighbour's Tasmota is never mistaken for it. Wherever you are asked to
  join it, its exact name is given.
- That `ESP_` name is only the one Tasmota's network has for a moment, as it
  turns it on and before it names it - which a computer can still list for a
  minute after, perhaps first. The network is now taken by its real name
  wherever that is listed, and caught in that moment it is looked at again,
  where before you could be asked to join one already gone.
- A power cut while the memory layout is rebuilt for Tasmota no longer costs
  Tasmota its settings. The rebuild wrote over where they were kept, so the
  plug came back waiting on its setup network, and the next run then stopped
  for good on the plug having changed. Now the settings are moved aside
  first, with an interim layout pointing at them, and the run itself waits
  for the plug, puts them back and tries again - up to three times in all,
  for a power cut, a read-back that differed, or the plug changing since its
  checks. A plug that did lose them is found on its setup network.
- A plug switched off and on before the installer has done its work goes
  back to Sonoff's firmware - by design, so nothing is lost - but the run then
  waited a quarter of an hour for an installer that was gone. It now sees
  Sonoff's firmware and sends the installer again, up to three times.
- A backup cut off by the home Wi-Fi going - the installer then raises its
  own network - is read again wherever the installer is once you have joined
  that, where before both tries went to its address on the home network and
  timed out, and an unattended run carried on without a backup.
- Tasmota sent through the installer whose upload took, though its answer
  was lost on the way back (a weak signal), joined your Wi-Fi by itself -
  and the try again waited a quarter of an hour for the installer, gone. It
  now finds Tasmota there, on your network or its own, and carries on.
- A plug whose Tasmota lost its settings part way through - switched off and
  on 7 times, say - waited on its own network while the run kept looking for
  it on yours, and once past setting Tasmota up (F4), the run asked for Wi-Fi
  details it could not use. It is now found on its own network and set up
  through it again, and Tasmota set up again where its template went too.
- Tasmota given the Wi-Fi on its setup network that took the details and
  restarted onto your Wi-Fi before saying so was sent them again, and the run
  then waited a quarter of an hour for you to join its network - gone. A
  network gone after no word is now taken as the plug on your Wi-Fi, and it
  is looked for there.
- An ESPHome plug whose API is encrypted, asked for its Wi-Fi without the
  key, could be taken for one without the tool's Wi-Fi action, where it
  answered and hung up before the tool's next write; the run then asked for
  the Wi-Fi password rather than for the key. Its answer is now read.
- A run started again (`--new`) after the memory layout was rebuilt for
  Tasmota, before official Tasmota was set up, took the plug for one on its
  way from ESPHome and sent it round steps that need Sonoff's layout, which
  never finished. This computer's record of the plug now tells the two
  apart, and the run goes on from official Tasmota.
- A plug offline in eWeLink when the installer is to be sent - after a send
  cut off part way, say - is waited for for up to three minutes, then the
  run says to switch it off and on, where before it was refused at once
  and the firewall blamed.
- A run stopped as the memory layout was committed - its window closed, say -
  whose plug then finished the commit by itself, carried on by checking the
  plug's network on the startup firmware, which cannot say it, and went back
  to the start of the conversion with no way forward. It now sees the commit
  done and carries on from there.
- After the installer could not be sent again (the plug offline in
  eWeLink), a plug slow to answer made the run ask you to join the
  installer's own network, which it had never raised. The next try now
  follows.
- ESPHome's page arriving damaged - a byte changed on its way - stopped the
  run with "something unexpected went wrong". It is now asked for again, as
  when the connection drops.
- A power cut while the official startup firmware was written back (G1)
  left the sector being written half done, and every later run refused to
  carry on - the conversion stuck for good, though the plug kept running
  Tasmota. That one sector is now written again.
- The installer gives up an upload that has gone silent for a minute. A
  computer going to sleep part way through sending it Tasmota (D1) - its lid
  closed, say - left the installer waiting for good: it answered nothing
  more, neither on your network nor on its own, until it was switched off
  and on.
- An upload of Tasmota to the installer cut off by the network is noticed
  after two minutes, not five, and tried again - through the installer's
  own network, if it has raised that and you have joined it.
- Tasmota sent through the installer's own network, after an earlier try
  cut off by the network had the installer save your Wi-Fi for it, joined
  your Wi-Fi by itself - while the run waited for a setup network it never
  raised, and asked you to join it. The run now sends you back to your usual
  Wi-Fi and finds it there; any wait for Tasmota's setup network also looks
  for Tasmota on your network.
- A Tasmota plug too busy to say its template in time - just started, or
  answering the run's look round the network - was taken for a Tasmota not
  yet set up as an S60, and sent down the wrong route, which never finished.
  It is asked again, and left to the next look rather than guessed at.
- A plug switched on as the run starts - offline in eWeLink, and not yet
  answering on the network - is waited for for up to a minute while it joins
  the Wi-Fi and the cloud, where before the run stopped at once to ask.
- An upload of official Tasmota to the startup firmware that stalls is
  noticed after two minutes, not ten, and tried again from a restart - up
  to three times - before it is left to be done by hand.
- The installer is now 4.4.3. It starts even when an upload of Tasmota into
  Sonoff's slot (D1) was cut off part way - by a power cut, or the computer
  stopping - where 4.4.2 refused to start, leaving a plug with no network at
  all that only a serial cable could recover.
- A run started again after D1 was cut off finds the plug on the installer
  and carries on from the backup, instead of looking for it in eWeLink -
  which it cannot answer - and being told to switch it off and on. An upload
  to the installer that stops is tried again by itself, three times in all.
- Ctrl-C no longer stops a run part way by itself. The first asks, at the
  end of the stage under way, whether to stop there - carrying on by itself
  after 30 seconds; a second within ten seconds stops at once, as before. At
  a question, or waiting on you, it stops at once. Closing the window stops
  the run, and the next one says that is why it stopped.
- Two runs on the same plug at once are refused, the second told which run
  has it: each would write over what the other was doing. The one refused
  leaves the other's log, report and firewall rule as they are, and
  `cleanup` waits until no run is converting a plug.
- Two plugs converted at once, in two windows, no longer trip over this
  computer's safety locks: a run starting put back a lock the other had
  lifted for its installer or its memory-layout change, and that one then
  failed putting it back itself. Each run that lifts one now says so, and the
  lock goes back when the last of them is done.
- This computer is kept awake while a run works, with what each system has
  already (systemd-inhibit, caffeinate, Windows' own setting) - nothing
  installed, and let go however the run ends.
- A plug that cannot be found can be set up through its own network by
  choice, for a computer that cannot see Wi-Fi networks. While waiting for a
  plug whose Wi-Fi went, the run says it will rejoin by itself, and that it has
  no network of its own at that stage.
- The mover, which takes a plug from Tasmota to ESPHome, raises its own
  network when it cannot join yours - two minutes after, if your Wi-Fi's name
  or password changed since the plug last joined - and the run sets it up
  through that, then gives ESPHome the same details.
- An upload Tasmota refuses - an image damaged on the way, say - is noticed
  from the page it answers with, and sent again, up to three times. Before,
  it was taken for one that worked, and the run waited five minutes for a
  restart that never came, then stopped blaming the network.
- A power cut in the second after official Tasmota's settings are reset
  (F4) leaves it without its Wi-Fi, on its own setup network. The run now
  sets it up through that there and then, where it waited three minutes and
  stopped saying the plug had not restarted.
- Stopping a run with Ctrl-C just as it added its firewall rule could leave
  the rule behind for good: the next run took it for one this computer had
  before, and left it. A stop there now waits for the rule to be recorded, so
  it is removed as usual, and a rule an earlier run left (ufw keeps the
  tool's comment on it) is recognised and removed too.
- Official Tasmota is taken as running only once it says so. A plug
  restarting back onto the startup firmware - the upload not taken - was
  taken for Tasmota for not answering for a moment, and the next stage then
  failed every time. Now the upload is sent again, up to three times, and a
  run carried on that finds the startup firmware at that stage sends it too.
- A plug switched off and on just as Berry Tasmota began to go on, from
  ESPHome or the mover (X2), starts the firmware it ran before - the startup
  firmware not settled in yet - which the run took for Tasmota, then waited
  for Tasmota in vain at every run after. It now sees which firmware is
  running, and sends the startup firmware again first.
- A run stopped a second into the commit (E3), which the plug then finished
  by itself, left a plug a new run (`--new`) took for one on its way from
  ESPHome: its steps need Sonoff's layout, and failed every time. The new
  run now sees the plug fetched the new layout and runs the startup
  firmware, and goes on from official Tasmota.
- Just after its restart into the high slot (D4), Tasmota could not yet say
  which slot it ran from; that was taken for the wrong slot, and a second
  upload sent into a plug still starting, which failed as "something
  unexpected". The run now waits until the plug reports which slot it runs
  from.
- Every firmware the plug runs part way through a conversion - the
  installer, the mover, the startup firmware, and Tasmota until it is set up
  - now goes by one name, `autoflash-recovery-` and the last four characters
  of the plug's hardware address (`autoflash-recovery-CDEF`): in your router's
  list, and for the installer's and the mover's own networks - and for
  Tasmota's, installed through the installer's own network, where it was
  Tasmota's default (`tasmota-ABCDEF-1234`). Every such network is open: the
  installer's had a password to type (`S60-OTA-Bridge-XXXX`).
- A converted plug is named for its firmware and the last six characters of
  its hardware address - `tasmota-abcdef` or `esphome-abcdef` - whatever kind
  of plug it is, and its own network has that name too. Tasmota was
  `tasmota-ABCDEF-1234`, and ESPHome `sonoff-s60-abcdef` with its network
  `Sonoff-Setup-CDEF`; the run still knows those. The names a person sees
  still say what plug it is: Tasmota's template (Sonoff S60TPG) and ESPHome's
  friendly name (Sonoff S60). ESPHome is 1.2.0 for it.
- Setting Tasmota up at the end (F4) restarts the plug once, where it
  restarted it twice: the name and the template go in as the plug saves the
  defaults its reset rebuilt, so it comes back converted, and never as
  Tasmota's own `tasmota-ABCDEF-1234` for a moment in between. Should that
  not take, they go in after the reset, as before.
- A Tasmota whose settings were erased part way - switched off and on 7
  times, as the run can advise - comes back under the run's name for it
  (`autoflash-recovery-CDEF`) once it is given the Wi-Fi on its setup
  network, where it kept Tasmota's own until the next firmware.
- The tool calls itself Autoflash, not the S60 conversion tool - in
  `--version`, and to the system it keeps awake - as it is to convert more
  than one kind of plug. Its firewall rule is called `Autoflash` (it was
  `S60 autoflash`); one a run of an earlier version left is still found and
  removed.
- What is the S60's own - its names in eWeLink and its stock firmware, its
  wiring for Tasmota, what its ESPHome calls itself - is kept in one place
  (`tools/autoflash/kinds.py`), and a run keeps which kind of plug it
  converts, ready for more kinds. Official Tasmota is still named after the
  plug's own model (Sonoff S60TPG), and the messages that only meant "the
  plug" now say so.
- ESPHome that cannot join your Wi-Fi at the end (I2) is set up through its
  own network by the run, as the mover already was, where before the run
  stopped and left it to you - and both carry on by themselves when you have
  joined that network already. The details the run has go first (right after
  a router outage, say); new ones are asked for only if the plug does not join
  with them. Every send of Wi-Fi details through a plug's own network, Tasmota's
  too, is tried up to three times, and rejected ones can be typed again.
- ESPHome, and the mover, no longer lose the Wi-Fi details the run wrote in
  when their first join takes over 30 seconds - a router restarting just then,
  say. ESPHome's `wifi.configure` put back what was there before - nothing, on
  a first boot - and saved that, leaving a plug with no network until it was
  switched off and on. They now start with the details, and keep trying.
- A run begun with 1.2.0 and stopped after its checks of the plug (E1), then
  carried on with this version, stopped at the next step every time: this
  version makes its own set of files for the plug, which those checks were
  not made with. The checks are now made again first, as the memory layout's
  rewrite already did.
- A run begun with 1.2.0 and stopped once the memory layout was being
  rewritten (E3) or after, then carried on with this version, stopped every
  time at a step 1.2.0 did not have: putting ESPHome's bootloader on (D5),
  which can only come before that rewrite. Such a run now leaves it out: the
  plug keeps Sonoff's bootloader, as 1.2.0 left every plug, and the final
  check does not look for ESPHome's. It goes on when the plug next changes
  firmware. Swapping 1.2.0's temporary startup firmware for the official one
  (G1) then asked for the Wi-Fi details the plug was converted with, to rebuild
  and check it - which no details could, as that firmware is not this
  version's. For such a run it is now written over without that check, once
  the plug shows the official memory layout and runs official Tasmota, which
  never starts from there; the final check reads the official one back.
- A converted plug left on Tasmota's own safeboot - after another firmware's
  whole-flash image (an ESPHome `factory.bin`, say) was uploaded through
  Tasmota's page, which writes it over Tasmota, so the plug starts the
  safeboot from then on; or just switched to it - was taken for one part way
  through a conversion, and sent down steps that need Sonoff's layout, which
  stopped at D4. The run now tells Tasmota's own safeboot from its own startup
  firmware, starts the plug's Tasmota again if it is there, else puts official
  Tasmota back, with its settings either way, and goes on from there to the
  firmware chosen - or stops there, for Tasmota. It looks for such a plug as
  for one it converted: one this computer converted, or the one `--plug-ip`
  names.

## 1.2.0

The report at the end now comes from every run that converted or failed, and
says how long each stage took.

- A run that failed - a stage that stopped, or something unexpected - offers
  its report too, saying where it stopped and why, with nothing private in
  it. A run you stopped yourself, at a question or with Ctrl-C, offers none.
- The report gives each stage's time, how long each stage that runs on the
  plug kept it busy, which stages needed retries, and the plug's Wi-Fi
  signal.
- Python has a line of its own in the report, saying where it came from:
  the tool's portable one, one it installed, a virtual environment, or one
  already on the computer.
- Fixes a plug that had just restarted, after the memory-layout rewrite say,
  being taken for one not answering: a request it could never answer was
  waited on for up to 20 minutes.

## 1.1.1

Fixes the ways a plug could be left answering nothing, not even its web
page, until it was switched off and on.

- A download that stops part way, as a router restarting mid-transfer leaves
  it, is given up after five seconds and fetched again. The plug used to wait
  on it for ever.
- Never gives up on a request the plug has not answered yet: a plug that
  found one given up on stopped answering altogether.
- No stage keeps the plug busy for more than about a minute. Busy for much
  longer, a real plug stops answering (its energy meter's readings pile up),
  so the plug now spends at most 20 seconds retrying, and this computer waits
  out a router restart instead - up to five minutes - and runs the stage again
  by itself.
- Notices a stage that has stopped sooner; no stage waits more than five
  minutes.

## 1.1.0

Keeps going through a router restart or a dropped network, says why when it
cannot, and backs up each plug's Sonoff firmware before it is replaced.

- Backs up the plug's whole flash - Sonoff firmware, settings and eWeLink
  identity - as a stage of its own before Tasmota is written, so it can go
  back to Sonoff over USB-serial one day (`--no-backup` keeps none). Uses the
  installer firmware 4.4.2.
- Keeps each plug's records under its own MAC address.
- Rides out a router restart: the plug waits up to five minutes for its
  Wi-Fi to come back and carries on, and makes no request while it is down.
- When a stage on the plug stops, it says why straight away, in the plug's
  own words, instead of waiting half an hour; and if the plug could not tell
  this computer, its console is read for the reason.
- Notices a plug that has stopped within seconds; no stage waits more than
  eight minutes.
- Tries again by itself, up to three times, a stage that failed for a
  network reason and is safe to repeat.
- Picks up where it left off when the memory-layout rewrite's report was
  lost, or the official startup firmware's install was cut off part way -
  both used to leave a plug that could not be finished.
- Says what to do with a plug that answers nothing - switch it off and on,
  which is safe at that point - and waits for one still finishing a stage
  that was stopped.
- Waiting for you, to join a network say, no longer times out.
- Says when the installer download stopped part way, rather than blaming a
  firewall.
- Windows: sets up Python without progress bars, and no longer crashes
  looking for an installed Python that is not there.
- The emulator: runs several plugs side by side (`--instance N`), and
  restarts the router faithfully (`plug router off/on`).

## 1.0.0

The first numbered release: a stock Sonoff S60 to the official Tasmota, over
the air, with nothing opened or soldered.

- One command from start to finish (`start.sh`, or `start.cmd` on Windows),
  which also sets up the Python it needs.
- The UK S60TPG and the European S60TPF, on stock firmware 1.1.1 and 1.2.0.
- Ends byte for byte identical to a plug flashed over USB with the official
  Tasmota release, and checks that by reading the flash back.
- Resumable at any point, with every step checking the plug before it writes.
- Takes the plug's Wi-Fi from the plug itself; the password is never written
  to this computer's disk.
- Adds one temporary firewall rule for the plug when needed, and removes it
  again however the run ends.
- Retries Safeboot block transfers for up to a minute when the network drops.
- An unattended mode for testing (`--unattended`), and an emulated plug to
  test against (`tools/emulator/`).
- The tool's version in its log, in the test report, and from `--version`.
