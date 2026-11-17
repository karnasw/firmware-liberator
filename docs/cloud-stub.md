# Local cloud stubs for the emulators

Each emulator gives its device an eWeLink "cloud" of its own, served locally,
so a conversion runs without the real eWeLink cloud and several run side by
side. Both stubs are one server,
[`tools/ewelink/stub_cloud/stub_server.py`](../tools/ewelink/stub_cloud/stub_server.py),
run inside the emulator's own network: nothing touches the real eWeLink cloud,
a real device or the read-only stock dump.

- [The Espressif emulator's stub](#the-espressif-emulators-stub) is chosen with
  `emu.sh --stub-cloud`. Without it that emulator runs against the real cloud.
- [The Beken emulator's stub](#the-beken-emulators-stub) is always on: that
  emulator has no real-cloud mode.

Starting either emulator is covered in [emulator.md](emulator.md). This page
says what each stub does, what in the firmware it rests on, and what it has
been shown to do.

## The Espressif emulator's stub

### Why a stub

Emulator instances are separate in everything but one. Each has its own
network, container and state (`--instance N`: `autoflash-emu-N`, `state-N`,
port `8080+N`), but against the real cloud they share eWeLink, which keeps one
connection for each device id. Every emulated device is a clone of one real
device, with its device id, so only one can be online at a time, and the part
of each run that needs eWeLink has to take turns. With `--stub-cloud` each
instance has a cloud of its own: nothing is shared, runs go side by side, and
no eWeLink account is needed.

### What a conversion needs from eWeLink

Less than it seems. Followed through the tool's code (`step_discover` in
[`account.py`](../tools/autoflash/account.py), `step_bridge` in
[`installer.py`](../tools/autoflash/routes/espressif/installer.py)), eWeLink
is needed for two things:

- **The tool asks the account API** (`*-apia.coolkit.cc`, through
  [`get_device_key.py`](../tools/ewelink/get_device_key.py) and
  [`query_ota.py`](../tools/ewelink/query_ota.py)). It signs in to fetch the
  device's LAN key, which the device's local messages are AES-encrypted with
  and which the cloud hands out, and to read the device's model and firmware
  version and see that it is online.
- **The device holds a connection to eWeLink** (`dis_server`, over TLS). The
  stock firmware asks its dispatch host for an IoT server, opens a WebSocket
  to it and stays connected. It opens its LAN interface only while it believes
  it is online.

Both clash on clones, as eWeLink keeps one connection and one account record
for a device id. The stub replaces both, for one instance.

The installer itself never goes through the cloud.
[`serve_tasmota_ota.py`](../tools/espressif/serve_tasmota_ota.py) (through
[`ota_push.py`](../tools/ewelink/ota_push.py)) connects to the IoT WebSocket
as the app does and sends an `upgrade` command naming a download address on
your computer. The cloud passes that small message to the device, which
fetches the file over plain HTTP on the local network.

A conversion from Sonoff's firmware needs eWeLink up to C2, and again at C3 or
D1 only to send the installer again, when the device has gone back to Sonoff's
firmware (switched off and on before the installer had done its work). Once D1
is done and the device runs the interim Tasmota, the rest uses the local
network alone, the same with `--stub-cloud` as with `--cloud`. A run back to
Sonoff's firmware uses eWeLink again, for the device's firmware where there is
no backup and to check the relay; so does a readout.

### How it works

The emulated device is pointed at a dispatch server on a private domain that
exists only inside the emulator, served by the stub under a certificate from a
CA of the stub's own, which the working copy of the firmware is made to trust.
The device comes online against the stub, and the tool reads the device's key,
already in the dump, from the stub.

The domain's names are resolved only by the emulator's `dnsmasq`, and `.test`
is reserved (RFC 6761), so none can clash with a real one:

- `disp.autoflash-stub.test`, the dispatch host (and
  `disp-v1.autoflash-stub.test`, for the MINIR4);
- `iot.autoflash-stub.test`, the IoT host that dispatch hands back;
- `apia.autoflash-stub.test`, the account API.

All resolve, through `/etc/hosts` lines stub mode adds (which `dnsmasq`
reads), to the router's address on the instance's network, `192.168.60.1`,
where the stub listens. They only have to be other than coolkit's, and to
match the certificate's names and `dis_server`.

#### What stub mode does not do

This is hosting your own device on your own server, not standing in for
eWeLink:

- It never resolves `coolkit.cc` or `coolkit.cn` names to the stub. They are
  not in its certificates, and the container points eWeLink's account API and
  app dispatch hosts at `0.0.0.0`, so they lead nowhere.
- It never uses or forges coolkit's certificate or CA private key. The stock
  firmware cannot be intercepted without that key; the stub changes which CA a
  copy of the firmware trusts instead.
- It never extracts or needs the device's own client key: the stub's TLS asks
  for no client certificate.
- The stock dump is never modified. Every change is made to the instance's
  working copy (`captures/emulation/state-N/`), which `--fresh` builds again
  from the backup.
- The CA's private key stays in `captures/emulation/stub-cloud/`, which Git
  ignores, and never enters the firmware. The device's keys are read only
  inside the container and redacted in logs, and none is written in the docs
  or a commit.

A trust-store edit and a server answering the dispatch protocol are also how
a TLS interception is built, so the stub is kept to the firewalled network of
its instance, and a change to it deserves a careful review.

### The firmware facts it rests on

Read from the dump of a stock S60 on Sonoff 1.2.0, unless another device is
named. The dump holds the device's eWeLink keys and Wi-Fi password in plain
text. No such value is written here: where one matters, its field is named.

The dump is `captures/devices/<mac>/sonoff-backup/flash.bin` (ESP32-C3, 4 MB,
[S60 style](espressif-research.md#flash-and-layouts); `report.json` beside it
lists the partitions). The app read is the one `otadata` selects, in `ota_0`
at `0x20000`: `esp32_fw`, v1.0.0-21-g28df76c, built Feb 12 2025 on ESP-IDF
v4.4.2. After a run, `ota_1` at `0x210000` holds the installer,
`autoflash_bridge`.

#### Dispatch and the IoT connection

The app's strings show the exchange:

- `%s-disp.coolkit.cc` builds a dispatch host from a region. Others are
  `disp.coolkit.cn`, `cn-disp.coolkit.cn`, `cn-gsm.disp.coolkit.cn` and
  `test-gsm.disp.coolkit.cn`.
- `POST /dispatch/device HTTP/1.1` is the dispatch request, its body
  `{"accept":"ws;%u","version":%u,"ts":%u,"deviceid":"%s","apikey":"%s","model":"%s","romVersion":"%s","fwVersion":"%s"}`.
- `GET /api/ws HTTP/1.1`, `HTTP/1.1 101 Switching Protocols` and `hbInterval`:
  the IoT channel is a WebSocket at `/api/ws` with a heartbeat.
- `dispatchUrl`, `dispatchPort`, `resetDispatch` and
  `reassemble_and_dispatch` are not exercised: the reply the device acts on
  carries `IP` and `port`.

What the device was seen to do with them, in the emulator:

- **It dials the host stored in NVS**, `frame_params.dis_server` and
  `dis_port`, not the one the string builds. In a capture of a clean stock
  start with no internet (`--pcap`, about 2 minutes) it asked DNS again and
  again for `eu-dispd.coolkit.cc` (`dispd`), the stored value, and never for
  `eu-disp.coolkit.cc`. Refused, it tried again after 5 to 90 s. So sending it
  to the stub is an NVS edit, with no firmware patch.
- **It asks the time of nobody first.** No NTP query came before the lookup,
  so the moment DNS answers it connects, whatever its clock says. The stub's
  certificates are therefore valid from a date long past.
- **It takes the IoT server as a numeric address.** It `inet_aton`s the
  reply's `IP` and makes no DNS lookup: a hostname became `INADDR_NONE`, and
  it dialled `255.255.255.255:443`. The stub's leaf therefore names its
  address as well (`IP:192.168.60.1`).
- **The `register` answer must echo its `d_seq`** (a number, not `sequence`)
  and carry `config {"hb":1,"hbInterval":145}`. Without the echo it dropped
  the WebSocket after about 10 s and dispatched again. `date` wants the date;
  `update` and `query` are content with a bare `error:0`.
- **Online, it opens its LAN interface.** On one WebSocket it sent `register`,
  `date`, `update` (`init:1`, the last update's result, `fwVersion`), its
  answer to the last command, `update` (`ssid`, `bssid`) and `query`. It then
  advertised `_ewelink._tcp.local` over mDNS (`SRV …:8081`, TXT
  `encrypt=true`, `type=device`) and opened port 8081, which it does only
  while online.

#### What it trusts

The app pins one self-signed coolkit root CA, held twice, byte for byte:

- Subject and issuer `C=cb, ST=gd, L=sz, O=coolkit, OU=dev, CN=coolkit.cn`;
  valid `2017-07-11` to `2117-06-17`; key RSA-1024, SHA-1; the PEM's SHA-256
  starts `b81c05a5…`.

| Copy | Flash offset | In `ota_0` | Slot |
|---|---|---|---|
| 1 | `0x38d4c` | `0x18d4c` | 1040 B: a 1036-B PEM, then `\r\n\x00\x00` |
| 2 | `0x39ed8` | `0x19ed8` | the same |

Each slot is a NUL-terminated PEM string of 1040 bytes with unrelated data
straight after it (`GET %s?deviceid=…`, for one), so a replacement must be
shorter than 1040 bytes, padded with NULs; mbedTLS stops at `END CERTIFICATE`.
An EC P-256 CA's PEM (about 644 B) fits, and so does an RSA-1024 one; an
RSA-2048 one (about 1200 B) would not.

The S60 takes either kind. With an EC P-256 CA swapped in, and
`openssl s_server` serving the leaf on `192.168.60.1:443`, a capture showed a
whole TLS 1.2 handshake (ClientHello; ServerHello, Certificate,
ServerKeyExchange, Done; ClientKeyExchange, ChangeCipherSpec, Finished;
NewSessionTicket, ChangeCipherSpec, Finished) with no reset after the
certificate, on `ECDHE-ECDSA-AES256-GCM-SHA384`, and then an encrypted record
of 327 bytes, its `POST /dispatch/device`. The patched image differed from
stock only in the two slots, the two Bluetooth patches and its SHA-256, and
the bootloader accepted it. Other firmware is stricter: a classic ESP32's
gives the task holding the connection only the stack an RSA check takes, and
the BASICR4's turns an EC CA down (`unknown_ca`). So a firmware that pins
eWeLink's RSA CA, the S60's included, is now given an RSA-1024 one
([how](../tools/espressif/emulator/README.md#the-dump-patch_stockpy-rewrite_nvspy)).

#### Its identity in NVS

`frame_params` (a JSON blob in the NVS namespace `frame_params`, key
`content`) holds the device's identity and network. Not secret: `uiid` `190`
(eWeLink's device type for the S60), `model` `SN-ESP32C3-S60-01`, `dis_server`
`eu-dispd.coolkit.cc`, `dis_port` `443`, `is_encrypt`, `factory_uart_flag`,
`sledOnline`. Secret, and left in the dump: `deviceid`, `apikey`,
`encrypt_key`, `sha256`, `ssid`, `passwd`.

Other namespaces: `nvs.net80211` (ESP-IDF's `sta.ssid` and `sta.pswd`, which
the emulator rewrites); `data_backups`, `user_usual` and `user_unusual` (a
second copy of `deviceid` and `apikey`, with `finalUserApikey`, `flashCipher`,
and energy and relay settings); `phy` (`cal_data`, `cal_mac`, `cal_version`);
`productId` (an opaque 14-digit number, not the product model);
`histEnergy0..8`, `bt_config.conf` and `evtLogName`. eWeLink's product model
(`S60TPG`, `S60TPF`) is nowhere in the flash. In this dump `apikey` and
`encrypt_key` hold the same value and either opens the LAN advert, so which
of them is the LAN key does not matter for this device
(`device_identity.py --lan-key-field` chooses where they differ).

#### What it says of itself on the network

Its LAN advert is mDNS `_ewelink._tcp`, with a TXT record of `txtvers`, `id`,
`type`, `apivers`, `seq`, `encrypt=true`, `iv`, and `data1`, `data2`. Its
state is in there encrypted: AES-128-CBC, keyed with the MD5 of the LAN key,
the IV in `iv`, the ciphertext in base64 split over `data1` to `data4`.
Opened, it holds `configure`, `current`, `fwVersion`, `power`, `pulses`,
`rssi`, `sledOnline`, `staMac`, `switches`, `triggerType` and `voltage`. The
tool does not read it, as the firmware advertises only with LAN Control on in
the app: it takes the Wi-Fi signal from the device's eWeLink record
(`params.rssi`) instead, which the S60 reports to the stub too. The same
`rssi` key sits with `fwVersion`, `staMac` and the meter readings among the
app's strings for its full state report, which never came in a stub session
(each lasted under a minute before the installer went). The app's serial
factory-test command `AT+WIFI_RSSI?` cannot be reached over the network.

#### Where other devices' firmware differs

- **The BASICR4** (Sonoff's ais SDK) takes only compact JSON, with no space
  after a colon: `{"error": 0}` made it dispatch again, over and over. So
  every answer is compact, as eWeLink's must be, though that has not been
  seen byte for byte (the traffic is under TLS). The S60 and S61 take either.
- **The MINIR4** connects by dispatch's `domain`, where eWeLink gives a
  hostname, and checks the certificate against it: its mbedTLS (ESP-IDF 4.4)
  cannot match an IP address in one. It also puts `-v1` into the dispatch
  host it was given. So the stub answers `domain` with a name
  (`--iot-domain`), and the emulator's DNS knows
  `disp-v1.autoflash-stub.test`.
- **An ais device** has no `frame_params`: the stub knows it by its factory
  record ([Espressif research](espressif-research.md#sonoffs-ais-sdk)), and
  takes its factory key for the LAN key, which no ais device has been seen
  using yet.
- **The POW Elite and POWCT** report their relay only when it changes. With
  none in the account record, the tool's relay check back on Sonoff's
  firmware waited three minutes and gave up.
- **The TH Elite** (Sonoff 1.0.2) reports one `switch`, and answers a
  `switches` list with error 400.
- **The S61** asks its time zone after it registers, and closes the channel
  when it is not told.

### What is changed in the device's flash

Both changes are made to the instance's working copy as `emu.sh` builds it.

- **The CA and the stub's certificate.**
  [`make_ca.py`](../tools/ewelink/stub_cloud/make_ca.py) makes them into
  `captures/emulation/stub-cloud/`: an EC P-256 root CA, and a leaf it signs
  for `disp.autoflash-stub.test`, `iot.autoflash-stub.test`,
  `*.autoflash-stub.test` and `IP:192.168.60.1` (the address the device and
  the tool's app side connect to). With `--rsa`, which `emu.sh` gives for a
  firmware pinning eWeLink's RSA CA, the CA is RSA-1024 and kept in
  `captures/emulation/stub-cloud-rsa/`. Both certificates run from 2020 to
  2125, so neither needs renewing or a set clock. The CA is flashed into the
  firmware, so it is kept once made (`--force` makes a new one) and only a
  leaf that no longer fits is issued again. Instances started together would
  each have made a CA where only one is flashed in, so it takes a lock and
  writes each file whole.
- **The trust swap.**
  [`patch_stock.py --trust-ca`](../tools/espressif/emulator/patch_stock.py)
  finds each coolkit CA the app pins, replaces it with the stub's, padded to
  its slot, and seals the image again (`appimage.reseal`) so the bootloader
  accepts it. It is done with the patches every emulated Sonoff app has, which
  skip Bluetooth start-up (QEMU emulates none) and touch neither dispatch nor
  TLS, so dispatch and trust behave as on real hardware. The routines are
  found by what they call; on the S60's 1.2.0 they are the two at
  `0x4200A66A` and `0x4201BB60`, made to return 0.
- **The dispatch address.**
  [`rewrite_nvs.py --dis-server disp.autoflash-stub.test --dis-port 443`](../tools/espressif/emulator/rewrite_nvs.py)
  sets those `frame_params` fields as it sets `ssid` and `passwd`: the
  partition is rebuilt with `nvs_partition_gen`, so the value's length is
  free.

### The stub server

[`stub_server.py`](../tools/ewelink/stub_cloud/stub_server.py) is one HTTPS
listener on `192.168.60.1:443`, with Python's standard library alone. It logs
every message with secret values (`apikey`, `deviceid`, `devicekey`,
`encrypt_key`, `staMac` and the like) redacted. Each connection's TLS
handshake is made in its own thread with a 30 s timeout, so one stalled
client cannot stop the rest.

- **`POST /dispatch/device`** (the device) answers with itself as the IoT
  server: `IP` numeric, `domain` a name.
- **`GET /api/ws`** is a minimal RFC 6455 server, pings answered. A device's
  `register` gets its `d_seq` back with the heartbeat `config`; `date` the
  date; a `query` naming `TZ` the time zone (UTC); `update` and anything else
  `error:0`. A device silent for longer than twice the heartbeat and 15 s is
  offline.
- **`GET /dispatch/app`** (the tool) gives the same server for the app side.
- **The relay.** Device and app each hold a WebSocket, and the stub passes an
  app's command to the device by its `deviceid` and the device's messages
  back. Without it the `upgrade` never reached the device. An app watching is
  told `sysmsg {"online": false}` when the device drops, as it does to
  install, as at eWeLink.
- **`POST /v2/user/login`, `GET /v2/device/thing`, `POST /v2/device/ota/query`**
  (the tool) are the account API: a token, the device list and the update
  offer. Each sign-in gives a new token and ends the one before, as at
  eWeLink.
- **`GET /ota/user1.bin?deviceid=<its id>`** serves the offered firmware.

**The account record** is what makes the tool behave as it does against
eWeLink. Its identity is read from the booted dump by
[`device_identity.py`](../tools/ewelink/stub_cloud/device_identity.py), inside
the container and into the container's `/run`, so the LAN key never leaves
the instance's network.

- `extra.model` is the firmware's own model (`SN-ESP32C3-S60-01`), by which,
  or by the product model, the tool knows what kind of device it is.
- `devicekey` is the LAN key (`get_device_key.py` fails without one).
- `online` is true only while the device's WebSocket is held. The tool waits
  on it before sending the installer, and by it tells a device cut off from
  the internet (`why_not_found`).
- `params` are what the device itself reported in dispatch, `register` and
  its `update`s (`fwVersion`, `ssid`, `bssid`, `only_device`), as eWeLink
  keeps them. `params.ssid` names its network in "not seen on this computer's
  network". The tool needs `params.fwVersion`, as `query_ota.py` does. They
  are kept in the instance's `stub-reported.json`, and saved and restored with
  a device (`unattended-test.sh --save`, `--from`; `--fresh` forgets them), so
  a device converted since, which never speaks to the cloud again, is listed
  with the version it last ran, as eWeLink lists it.
- `params.staMac` is how the tool finds the device on the network, as
  Sonoff's firmware need answer nothing there. The device never reports its
  MAC on this channel, so it is the MAC `emu.sh` runs the device with
  (`DEVICE_MAC`, `--sta-mac`).
- `params.switches` is the relay off until the device reports it; from then
  on only what the device reported, in its own shape.
- `productModel` (`S60TPG` UK, `S60TPF` Europe) names the region in the
  report and the Tasmota template. It is eWeLink's name, given at pairing,
  and not in the dump. `emu.sh` takes the one eWeLink gave this device from
  the tool's records of earlier runs (`devices/<mac>/runs/*/autoflash-state.json`:
  a real conversion's, a kept device's or an instance's), or from
  `AUTOFLASH_EMU_STUB_PRODUCT_MODEL=S60TPG|S60TPF`. With neither, none is
  served and the report leaves "Model" blank.

A `register`'s log line says whether its id and key are the account's own,
without showing either.

**The update offer.** To a query with the device's own version the stub
offers nothing (`data.otaInfoList` empty, which the tool needs present). To
one with an older version, as the tool asks for eWeLink's latest, it offers
the device's own Sonoff app, patched as the running one is and in Sonoff's
wrapper: `emu.sh` makes it into the instance's state
([`firmware.py`](../tools/ewelink/stub_cloud/firmware.py)). A run back to
Sonoff's firmware with no backup fetches it.

**The account's other devices.** A real account often holds more, and a
readout of every device reads them all. Set `AUTOFLASH_EMU_STUB_OTHERS` to a
JSON file listing made-up ones, each
`{"deviceid", "name", "productModel", "model", "uiid", "fwVersion"}`, and
`emu.sh` copies it into the instance's state, where the stub reads it
(`--others-json`, default `/state/stub-others.json`). They are listed after
the device, offline, with a made-up key and no firmware offered. None is
emulated, so only a readout's first stage, which asks eWeLink alone, runs on
them.

#### Every exchange, with examples

The shapes were first taken from the firmware's strings and the project's
eWeLink client ([`serve_tasmota_ota.py`](../tools/espressif/serve_tasmota_ota.py),
[`websocket_minimal.py`](../tools/ewelink/websocket_minimal.py)), then
confirmed live: the stub logs every message, so each run is also a capture.
These are from one stub run's log, in the order they came. The log prints
JSON with spaces; on the wire the stub's answers are compact. Values that
identify the device or the account are `<…>`: the device id is 10 hex digits
starting `10`, the apikey and the LAN key are UUIDs, and the chip id is 12 hex
digits. Your computer and the stub are at `192.168.60.1`, the device at
`192.168.60.50`.

**The device and the stub**, each time the device starts:

| message | the device sends | the stub answers |
|---------|----------------|------------------|
| dispatch, `POST /dispatch/device` | `{"accept": "ws;2", "version": 3, "ts": 34738, "deviceid": "<device id>", "apikey": "<apikey>", "model": "SN-ESP32C3-S60-01", "romVersion": "1.2.0", "fwVersion": "1.2.0"}` | `{"error": 0, "reason": "", "IP": "192.168.60.1", "domain": "iot.autoflash-stub.test", "port": 443}` |
| `register`, first on `GET /api/ws` | `{"userAgent": "device", "version": 8, "romVersion": "1.2.0", "action": "register", "chipid": "<chip id>", "ts": "27433", "model": "SN-ESP32C3-S60-01", "apikey": "<apikey>", "deviceid": "<device id>", "d_seq": 60376}` | `{"error": 0, "deviceid": "<device id>", "apikey": "<apikey>", "d_seq": 60376, "config": {"hb": 1, "hbInterval": 145}}` |
| `date` | `{"userAgent": "device", "action": "date", "apikey": "<apikey>", "deviceid": "<device id>", "d_seq": 20710}` | `{"error": 0, "deviceid": "<device id>", "apikey": "<apikey>", "d_seq": 20710, "date": "2026-10-19T20:51:17.706Z"}` |
| `update`, its state | `{"userAgent": "device", "action": "update", "apikey": "<apikey>", "deviceid": "<device id>", "d_seq": 94076816, "seq": 2, "init": 1, "params": {"only_device": {"ota": "success", "ota_fail_reason": 0}, "fwVersion": "1.2.0"}}` | `{"error": 0, "deviceid": "<device id>", "apikey": "<apikey>", "d_seq": 94076816}` |
| an answer to the last command it had | `{"error": 0, "sequence": "1791445746113", "apikey": "<apikey>", "deviceid": "<device id>"}`, the same at every start: that `sequence` is the `upgrade` that installed the readout firmware the dump was read through (08:49 on 8 October; the dump was written at 08:50) | nothing |
| `update`, its network | `{"userAgent": "device", "action": "update", "apikey": "<apikey>", "deviceid": "<device id>", "d_seq": 5805, "seq": 2, "params": {"ssid": "<network name>", "bssid": "<access point MAC>"}}` | `{"error": 0, "deviceid": "<device id>", "apikey": "<apikey>", "d_seq": 5805}` |
| `query` | `{"userAgent": "device", "action": "query", "apikey": "<apikey>", "deviceid": "<device id>", "d_seq": 65519, "params": ["timers", "TZ", "sceneMaxPow", "sceneMinPow", "uploadDayKwh", "uploadMonthKwh"]}` | `{"error": 0, "deviceid": "<device id>", "apikey": "<apikey>", "d_seq": 65519}`, now with `params.TZ` as well |

**The tool and the stub**, from A2 to C2:

| message | the tool sends | the stub answers |
|---------|----------------|------------------|
| sign-in, `POST /v2/user/login` | `{"password": "stub", "countryCode": "+44", "email": "stub@autoflash-stub.test"}` (the placeholder account), signed: `Authorization: Sign <HMAC-SHA256 of the body>`, `X-CK-Appid: <app id>`. The stub checks none of it. | `{"error": 0, "data": {"at": "stub-access-token-<hex>", "rt": "stub-refresh-token", "region": "eu", "user": {"apikey": "<apikey>"}}}` |
| device list, `GET /v2/device/thing?num=0` | `Authorization: Bearer stub-access-token-<hex>` | `{"error": 0, "data": {"thingList": [{"itemData": {"deviceid": "<device id>", "devicekey": "<LAN key>", "apikey": "<apikey>", "name": "S60 (stub)", "online": true, "extra": {"uiid": 190, "model": "SN-ESP32C3-S60-01"}, "params": {"only_device": {"ota": "success", "ota_fail_reason": 0}, "fwVersion": "1.2.0", "ssid": "<network name>", "bssid": "<access point MAC>", "model": "SN-ESP32C3-S60-01", "staMac": "<device MAC>"}, "productModel": "S60TPG"}}]}}` |
| OTA query, `POST /v2/device/ota/query` | `{"deviceInfoList": [{"deviceid": "<device id>", "model": "SN-ESP32C3-S60-01", "version": "1.2.0"}]}` | `{"error": 0, "data": {"otaInfoList": []}}` |
| app dispatch, `GET /dispatch/app` | `Authorization: Bearer stub-access-token-<hex>` | `{"error": 0, "reason": "", "domain": "192.168.60.1", "IP": "192.168.60.1", "port": 443}` |
| `userOnline`, first on `GET /api/ws` as the app | `{"action": "userOnline", "at": "stub-access-token-<hex>", "apikey": "<apikey>", "appid": "<app id>", "nonce": "BucKkPAM", "ts": 1792443098, "userAgent": "app", "sequence": "1792443098404", "version": 8}` | `{"error": 0, "apikey": "<apikey>", "sequence": "1792443098404", "config": {"hb": 1, "hbInterval": 145}}` |
| `upgrade`, the installer | `{"action": "upgrade", "deviceid": "<device id>", "apikey": "<apikey>", "userAgent": "app", "sequence": "1792443098405", "params": {"model": "SN-ESP32C3-S60-01", "version": "4.4.3", "binList": [{"name": "user1.bin", "downloadUrl": "http://192.168.60.1:8088/user1.bin", "digest": "<SHA-256 of the image>"}, {"name": "user2.bin", "downloadUrl": "http://192.168.60.1:8088/user2.bin", "digest": "<SHA-256 of the image>"}]}}` | It passes this to the device as it is. Within 0.1 s the device sends `update` `{…, "d_seq": 153697133, "seq": 2, "params": {"only_device": {"ota": "start"}}}`, answered like any `update` and passed to the tool. Half a second later the device drops its WebSocket, and the stub tells the tool `{"action": "sysmsg", "deviceid": "<device id>", "params": {"online": false}}`. The device fetches the image from `downloadUrl` in ranges, each request carrying its device id, a `ts` and a `sign`. |

What the values are:

- `d_seq` is a number the device picks for each message, which the answer
  echoes.
- `sequence` is the app's time in milliseconds, as a string; the answer to a
  command carries the command's.
- `ts` is the app's time in seconds; the device sends a number of its own (as
  a string in `register`).
- `version` is `3` in the device's dispatch request, and `8` on the WebSocket
  from device and app alike.
- `upgrade`'s `version` is the installer's own, from its wrapper's header
  (`4.4.3`). `binList` offers it under both of eWeLink's names, `user1.bin`
  and `user2.bin`; the device fetched `user1.bin`.

### Running it

`--stub-cloud`, which cannot go with `--cloud`, turns all of it on:

- **`emu.sh --stub-cloud`** builds the stub flash, makes the CA, mounts the
  certificates read-only at `/stub-cloud` and sets `STUB_CLOUD=1`. Instances
  run from 1 to 99.
- **`container/entrypoint.sh`** adds the `/etc/hosts` lines, opens port 443
  on the LAN side of the firewall (ufw would refuse it; the block on the
  internet is separate iptables rules), reads the device's identity from its
  dump and starts the stub, as root with `-B` so that no bytecode only root
  could delete is left in your state directory. A stub that fails does not
  take the emulator down.
- **The tool** is pointed at the stub by `AUTOFLASH_EWELINK_API_BASE`, which
  replaces every region's account API (`get_device_key.py`) and the app
  dispatch (`WS_DISPATCH` in `probe_ota_command.py`), and trusts it by
  `SSL_CERT_FILE`: a bundle of the system's roots and the stub's CA, so
  firmware downloads still verify. Python's `ssl.create_default_context()`
  honours that already, so no code path is the stub's alone. With an RSA-1024
  CA it also needs `OPENSSL_CONF` set to the `openssl.cnf` `make_ca.py`
  writes beside the CA, as OpenSSL's default on Ubuntu refuses so short a
  key. The entrypoint exports all three for a conversion you start by hand.
- **The internet.** Your computer may reach it, so A5 (`step_check`)
  downloads the official Tasmota files as a new user's run does (`emu.sh`
  keeps the host's bundle, made for the real home Wi-Fi, out of the
  container). eWeLink's own names lead nowhere and the device stays on its
  own network. Up to C2 a run reaches no internet at all.
- **`unattended-test.sh --stub-cloud`** converts with no eWeLink account, and
  a placeholder `.env` the stub ignores.
- **`fault_suite.py --stub-cloud`** (or `AUTOFLASH_EMU_STUB_CLOUD=1`) drops
  the lock that makes emulators take turns at the cloud (`CloudQueue`), and
  runs every scenario on `AUTOFLASH_EMU_STUB_WORKERS` emulators from
  `AUTOFLASH_EMU_FIRST_INSTANCE` (default 1). Its starting devices
  (`stub-suite-*`) and results (`captures/emulation/suite-stub/`) are kept
  apart from the real cloud's: a pass on the stub is not a pass against
  eWeLink ([testing.md](testing.md#the-espressif-fault-suite)).

A stub run's flash differs from a cloud run's, so `emu.sh` records which a
device has in `state-N/.flash-mode` (one from before that, or any cloud
run's, is "plain"). Changing an instance from one to the other is refused,
never done by quietly writing a converted device's flash again: `--fresh`
starts it over, and `--save` and `--from` carry the mode with a kept device.

Other work may be using emulators on the same computer. Use an instance
number nothing else has (`docker ps | grep autoflash-emu`), never `--fresh`
one that is not yours, and set `AUTOFLASH_EMU_IMAGE` (`autoflash-emu-stub`,
say) to build and run an image of your own without replacing the shared
`autoflash-emu`. Afterwards, `docker stop autoflash-emu-N` and
`rm -rf captures/emulation/state-N`.

Logs are in `state-N/logs/`: `uart.log` and `stub.log`, each added to across
runs, and `wifi-*.pcap` with `--pcap`.

```sh
D=captures/emulation/state-N/logs
tcpdump -nn -r "$D"/wifi-*.pcap 'udp port 53'       # DNS queries
tcpdump -nn -r "$D"/wifi-*.pcap 'tcp port 443'      # TLS attempts
strings "$D"/wifi-*.pcap | grep -iE 'coolkit|\.test' # hostnames, without tcpdump
sed 's/\x1b\[[0-9;]*m//g' "$D"/uart.log | tail -80  # de-coloured console
grep -E 'ws <-|ws ->|relay' "$D"/stub.log           # the channel, secrets redacted
```

### What has been shown

All in the emulator, on an S60 unless said.

- **The stub alone takes a conversion through C2.** `get_device_key.py`
  signed in and saved the LAN key, `query_ota.py` read `SN-ESP32C3-S60-01`
  v1.2.0 `online:true`, and `autoflash/cli.py step {A0,A5,A1,A2,C2}` ran with
  nothing reaching coolkit. C2 took about 18 s: the stub's log shows the app
  dispatch and `relay app->device (upgrade)`, and the device's serial log
  `Loaded app from partition at offset 0x210000` and the installer joining
  the home network.
- **Clones are online together.** Two instances of one device (the same id
  and MAC) each held an `ESTABLISHED` WebSocket to its own stub, registered,
  with port 8081 open. What bounds a batch is the computer: one emulator a
  core at most ([testing.md](testing.md)).
- **A stub run matches a cloud run.** `unattended-test.sh --fresh --
  --firmware {tasmota,esphome}` converted with no retries either way: Tasmota
  in 16 stages, 4m 11s on the stub and 4m 13s on the cloud; ESPHome in 11
  stages, 2m 36s and 2m 38s. With the product model served, the stub run's
  report said `S60TPG` and named the template "Sonoff S60TPG", as the cloud
  run's did.
- **The installer is sent again through the stub.** The whole fault suite
  runs on it ([testing.md](testing.md#the-espressif-fault-suite)). After a
  power cut at C3 or D1, or the network gone at C3fb, the device went back to
  Sonoff's firmware, the installer went again through the stub (twice after
  the C3 cut), and the device converted.
- **A device with no clock restarts unseen.** The stub's device gets no NTP
  time, and that found a fault in the tool: a Tasmota run stopped at stage 14
  (F4) with "the device did not come back after its settings were reset", the
  device answering all along. The tool told boots apart by
  `StartupUTC#BootCount`, which read `#1` both times: `StartupUTC` is empty
  without a clock, Reset 4 sets the count back, and Tasmota counts a boot at
  10 s of uptime. Its other check, the uptime falling below what it was
  before the reset (10 s), missed too: the request the restart cut off was
  held its full 10 s, so the new boot first answered at 12 s.
  `wait_for_reboot` now also takes a device up for less time than the wait
  has lasted as restarted, and asks again when the restart cuts the question
  off. Three conversions side by side then passed (Tasmota in 4m 11s and
  4m 22s, ESPHome in 2m 35s); one met the race again (Reset 4 at 9.3 s of
  uptime, the first answer at 12.4 s), and its stage took 38 s where before
  it gave up after three minutes. Any device without NTP time was open to it.
- **The Wi-Fi signal is read before anything is written** (on an image with
  the emulator's signal fix,
  [lcgamboa/qemu #16](https://github.com/lcgamboa/qemu/pull/16)). From
  Sonoff's firmware the tool reads the advert at A2; from Tasmota, `Status 11`
  at L1; from ESPHome at X1, its `WiFi Signal` sensor
  (`/sensor/WiFi%20Signal` answered
  `{"id":"sensor/WiFi Signal","value":-18,"state":"-18 dBm"}`), or else the
  line its native API prints in its configuration dump
  (`Signal strength: -19 dB`) where the API has no encryption key. This
  project's ESPHome has the sensor and no key. An ESPHome giving neither is
  first read at D4, as the recovery Safeboot between gives none: it answers
  `Status 11`, `Status 0` and `WiFiScan` with `Unknown`, and shows no signal
  on its pages or console. D4, E1 and I1 read it again; a weak signal is
  asked about once a run, and the report gives the lowest reading.

  | Run | Readings, dBm | Reported |
  |---|---|---|
  | Sonoff to Tasmota | A2 -21, D4 -18, E1 -21 | -21 |
  | Sonoff to ESPHome | A2 -17, D4 -24, I1 -21 (another: A2 -21, D4 -23, I1 -24) | -24 |
  | ESPHome with no sensor, before the API read, to Tasmota | D4 -21, E1 -23 | -23 |
  | The same with the API read | X1 -23, D4 -19, E1 -22 | -23 |
  | ESPHome with the sensor to Tasmota | X1 -18, D4 -20, E1 -18 | -20 |
  | Tasmota to ESPHome | L1 -19 | |

  Tasmota's own reports gave -17, -22 and -19 dBm in other runs.
- **A readout of every device in an account** works, each readout sent to the
  API Worker's own code (`website/api/index.js` under Node, its R2 bucket a
  folder) through `AUTOFLASH_READOUT_URL`
  ([as `unattended-test.sh` sends them](unattended.md#in-the-emulator); its rate
  limit is the Worker's, two a minute from one address). With a BASICR4 or
  MINIR4 stand-in and three to six others (an NSPanel, an S60TPG-M, which is
  a Matter variant the tool has not seen, and made-up models), twelve runs,
  up to seven at once: every first-stage device was asked of `/wanted`, then
  sent on its own; the BASICR4 and MINIR4 were read in full after PROCEED,
  put back on Sonoff's firmware and sent. `s` at PROCEED passed the BASICR4
  by with nothing sent to it; `q` ended the run saying what was sent and what
  was not. A device in `ENOUGH` was answered "not needed", no readout made
  and no PROCEED asked. A first-stage manifest held its product, firmware
  model and version, and no device id or name.
- **The same as fault-suite scenarios** (`sonoff>readout-all`, five emulators
  at once) all passed. Of three alike devices from one sender, one was taken
  and two answered "held". Switched off or crashed during the read, the run
  stopped at the device with the two first stages sent; run again after the
  device was switched off and on, it read and sent all three and left the
  device on Sonoff's firmware. Switched off for a second, the read gave up
  after about five minutes of refused connections.

Three things those readouts found:

- Quick first stages reach two readouts a minute, and one answered 429 was
  kept unsent. `send()` now sends it again every 15 seconds; in five runs (22
  readouts) four 429s were each sent again and taken, and seven quick first
  stages met two more.
- A readout's "Reading the device's memory" had stopped being written to the
  run's log, so a fault timed by it never fired (NOT-FIRED), in both fault
  suites. It is logged again.
- The API's local database (`local-d1.mjs`) needs a Node whose `node:sqlite`
  binds numbered parameters (`?1`). 22.11, with `--experimental-sqlite`, and
  22.14 stopped at the first `/wanted` with "column index out of range", and
  the tool, unanswered, kept each readout unsent; 22.23.4 runs it.

### Limits

- **It tests the tool and the firmware it installs, not eWeLink's servers.**
  The real cloud remains the reference: keep some `--cloud` runs.
- **The record lasts as long as the instance.** After the container starts,
  the stub knows no version or params until the device's first dispatch,
  where eWeLink keeps the last it was sent. The device dispatches as soon as
  it boots and the tool needs it online anyway, so no run has met the gap.
- **`query` is not really answered.** The device comes online without timers
  or energy settings and nothing in a conversion asks for them; testing those
  would need real answers.
- **A token ends only at the next sign-in**, never with time, and a WebSocket
  already signed in stays open. The account API and a new `userOnline` refuse
  an ended token, which is what the fault suite's `token` fault tests.
- **The device has no clock**, as in the plain emulator: it stays offline, so
  Tasmota's `StartupUTC` stays empty. Nothing in the tool may lean on it.

### Where the code is

`tools/ewelink/stub_cloud/`: `stub_server.py`, `make_ca.py`,
`device_identity.py`, `firmware.py`. In `tools/espressif/emulator/`:
`patch_stock.py` (the trust swap), `rewrite_nvs.py` (`--dis-server`,
`--dis-port`), `emu.sh` and `unattended-test.sh` (`--stub-cloud`),
`container/entrypoint.sh` (the names, the firewall, the stub's start) and
`fault_suite.py`. In `tools/ewelink/`: `get_device_key.py` and
`probe_ota_command.py` (`AUTOFLASH_EWELINK_API_BASE`).

## The Beken emulator's stub

Every emulated Beken device runs against its stub: `beken-emu.sh device`
starts it. Sonoff's firmware comes ONLINE on it, and the tool's whole route
runs through it in every direction
([the Beken fault suite](testing.md#the-beken-fault-suite)).

It serves the same two needs as
[the Espressif one](#what-a-conversion-needs-from-ewelink): the tool signs in
to the account API to find the device and send its update (B1, B2), and the
device holds its dispatch and IoT connection, without which it takes no
update. A run back to Sonoff's firmware with no backup also fetches the
device's official file from the stub's update offer.

### How it works

The emulated device is on a Wi-Fi of its own, whose router, `.1`, is the
emulator's container: its DHCP, its DNS (every name is the router) and its
eWeLink. Every connection the device makes off the network is sent to the
stub, whatever address it dialled
([how it fits together](../tools/beken/emulator/README.md#how-it-fits-together)),
and nothing reaches the internet. The tool, on a computer on the same Wi-Fi
(`beken-emu.sh computer`), is pointed at the stub's account API
(`AUTOFLASH_EWELINK_API_BASE`) with its CA trusted.

What this firmware needs of its cloud differs from the S60's, each point
found in the emulator
([the cloud handshake](beken-research.md#the-handshake-to-online)):

- Its factory dispatch URL is plain HTTP on port 80 (`http://8.8.8.8:80`, a
  placeholder), so dispatch is served unencrypted.
- The dispatch reply must be compact JSON, with a `domain` key and `port` a
  string.
- The IoT WebSocket is `wss://` with the certificate verified: the leaf must
  carry a `dNSName` SAN for the address dialled and a serverAuth EKU.
- It has no CA in flash to swap. It builds its CoolKit CA in RAM, so the
  stub's CA is put in where the firmware loads that root, at
  `mbedtls_x509_crt_parse` (`cloud_online.StockHooks`). No check in its
  verify path is changed, only which root it trusts, and only while Sonoff's
  firmware is the one that starts.

### The stub server

[`tools/beken/emulator/cloud_stub.py`](../tools/beken/emulator/cloud_stub.py)
runs the shared server on the router's address: dispatch in plain HTTP on
`:80`, the IoT WebSocket and the account API over TLS on `:443`. Its CA is
made once into the device's state directory (`ca.crt`, the leaf beside it);
the leaf is made again for another address, as the device checks that it
names the one dialled.

- **The account record.** The device's identity is what it says as it
  registers. Its MAC, which eWeLink's record carries, is read from the
  factory record in its flash (`0x3e0000`). Its model is the one in the stock
  app it first started, not the one it registers with, which comes from its
  sealed factory data: a flash rebuilt on the S61's settings for another
  model ([rebuild_dump.py](../tools/beken/README.md#rebuilding-a-dump)) says
  it is an S61, and the stub gives the other model.
- **The update offer.** As eWeLink offers a real device its official file,
  the stub offers the device's own stock app, packed as the RBL its
  bootloader applies, in the FOTA container its updater takes, named as its
  kind's are. A readout reverts with it. Both it and the model are made once,
  from the Sonoff firmware the device first started on, as it may next start
  on another.
- **What devices said of themselves** is kept across restarts
  (`stub-reported.json`), as an eWeLink account keeps its devices while they
  run another firmware. `beken-emu.sh device` returns once the device is in
  it.

### Running it

`beken-emu.sh device` runs the stub as root in the device's container
(`device-net.sh`), as it binds the router's ports 80 and 443. Its log is
`stub.log` in the device's state directory (`captures/emulation/beken/state/`,
or `state-N/` with `--instance N`). By hand:

```sh
python3 -I -B tools/beken/emulator/cloud_stub.py --state DIR --bind ADDRESS --flash DIR/flash.bin
```

`cloud_online.py`, the research run that first brought the S61 ONLINE, stands
the same server up on loopback instead, with `--plain` dispatch
([tools/beken/README.md](../tools/beken/README.md#driving-the-emulator)).

### What has been shown

`cloud_online.py`'s log shows the handshake: dispatch accepted, the CA
swapped once, the WebSocket open, `register`, and ONLINE with heartbeats
([the log](beken-research.md#the-handshake-to-online)). Each
official file of the S61's generation comes ONLINE on it too
([one code path](beken-research.md#one-code-path-for-every-4-mb-ewelink-build)).

### Where the code is

`tools/beken/emulator/cloud_stub.py` (the stub, on
`tools/ewelink/stub_cloud/stub_server.py`), `device-net.sh` (the router:
DHCP, DNS, the redirect to the stub, and the stub's start) and
`cloud_online.py` (the trust-anchor swap, the compact JSON and the
certificates).
