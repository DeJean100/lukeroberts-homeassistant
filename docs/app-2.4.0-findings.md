# Luke Roberts Android app 2.4.0: findings

Static analysis of `eu.lukeroberts.lukeroberts` 2.4.0 (version code 273),
decompiled with jadx. Nothing was executed. Native Kotlin app, R8
obfuscated. Statements marked *guess* are not verified on a lamp.

## Servers

| Host | Purpose |
| --- | --- |
| `https://cloud.luke-roberts.com` | App backend: OAuth, phone as "bridge", state reports, scene sharing |
| `https://luke-roberts-firmware.firebaseapp.com` | Firmware lookup (needs an app specific `Api-Key` header) |
| `https://luke-roberts-support.firebaseapp.com` | Support form, user lookup |

Cloud paths used by the app (not the public third party API):

* `POST api/v1/bridges`, `DELETE api/v1/bridges/{bridgeId}`
* `PUT` / `DELETE api/v1/bridges/{bridgeId}/lamps/{serial}`
* `PUT api/v1/lamps/by_serial_number/{serial}/state` with
  `{color_temperature, brightness, is_on}`
* `POST api/v1/scenes`, `GET api/v1/scenes/{uuid}` (scene sharing)
* OAuth: `oauth/authorize` (scope `lamp-control`, redirect `lr-auth://success`),
  `POST oauth/token`

No `/api/v2` paths exist. The public `/lamps` and `/command` endpoints are not
used by the app.

## Firmware

* `GET api/v1/firmware/latest?app=Android&model=&firmware=&revision=&serial=&bootloader=&homekit=`
  returns `{version, file, releaseNotes}`; `file` is a URL to a Nordic DFU ZIP.
* `GET api/v1/firmware/recovery?id=`, `POST api/v1/firmware/usedby/{serial}`.
* The endpoint needs an API key embedded in the app. It is not reproduced
  here; using it from Home Assistant would need permission from Luke Roberts.
* OTA runs over BLE: indications on `8E400001-F315-4F60-9FB8-838830DAEA50`,
  write `0x01`, the lamp reboots into the Nordic Secure DFU bootloader
  (advertises `0000FE59`) and the Nordic DFU library flashes the ZIP.

A lamp stuck in the bootloader after a failed update only advertises
`0000FE59`, not the control service. Home Assistant will not discover it then.

## Bluetooth

The app scans for service `44092840-0567-11e6-b862-0002a5d5c51b` or
`0000FE59` (bootloader). It does not filter on name or manufacturer data.

| UUID `4409284x-0567-11e6-b862-0002a5d5c51b` | Purpose |
| --- | --- |
| `…41` | Settings record (read/write, 36 bytes) |
| `…42` | External API; response notification for every write |
| `…43` | Scene records |
| `…44` | Selected scene id (1 byte; notify/read/write) |
| `…45` | Live light model / scene preview |
| `…46` | PIN unlock and change |
| `…47` | Status, errors, events |
| `…48` | Orientation |

Writes to any of these characteristics are answered by a notification on
`…42`; byte 0 is the status (`0x00` OK, `0x85` scene memory full,
`0xFC`/`0xFD` locked).

API frame: `A0 <api> <opcode|flags> [PIN u16 if flag 0x40] [id u16 if flag 0x80] <data>`.

## Brightness

* The app has no brightness control of its own. `03` and `08` only appear in
  `BLEValueProxyPayloadBrightness`, which relays cloud commands while the
  phone acts as bridge (silent push). Fields: `brightness: Int`,
  `relative: Boolean`.
* `relative = false` sends `A0 02 03 PP`, `relative = true` sends
  `A0 02 08 PP`; the value is written with a plain `int-to-byte`.
* A second payload `BLEValueProxyPayloadPowerLevel` sends `A0 01 03 PP`, so
  the lamp seems to accept opcode `03` with API byte `01` and `02`.
* The app labels relative commands with `cloud_command_brightness_relative_up`
  ("%d%% brighter") when the value is > 0 and `..._relative_down`
  ("%d%% darker") otherwise. It treats the byte as a signed change, while
  the Lamp Control API v1.3 says the brightness is multiplied by `PP/100`.
  Tested on a real lamp: the app is right. `A0 02 08 EC` (−20) dims,
  `A0 02 08 14` (+20) brightens; the multiplier reading (`50` / `7D`) does
  not dim.

## Scenes

Scenes are written as raw records to `…43`. Multi-byte numbers are **little
endian** (the static analysis suggested big endian; the capture below shows
otherwise).

* Header (8 bytes): `01, sceneId, sortIndex, wireType, int32 groupId`
* wireType 1, static scene (178 bytes): header, u16 downlight kelvin,
  uplight 16 segments × `[sat, u16 hue*65535 (or kelvin if sat=0), bri]`,
  downlight 84 bytes per spot brightness (rings of 0/3/6/…/21), 20 byte name
* wireType 2 (108 bytes): header, u16 kelvin, 78 zero bytes, name. Seen on
  the built-in dynamic scene "Demo" (a rotating "lighthouse" uplight); the
  animation is not in the record, so it must live in the firmware. Users
  cannot create these scenes in the app. Treat them as read-only: never
  rewrite, copy or delete them.
* wireType 3 (28 bytes): header and name (the "Off" scene, id 0)
* wireType 0 (8 bytes, header only): delete; also how empty slots are reported
* New scenes are written with sceneId 254.

Preview without saving: write `64, u16 kelvin, 64 B uplight, 84 B downlight`
to `…45` (*guess*, not captured yet).

### Verified on a lamp (Model F, firmware 1.2.2, app on Android 17)

Bluetooth HCI capture of the app creating, renaming and deleting a scene,
and a Sync from a PC (2026-11-18):

* **Create:** the app writes a full 178-byte record with header
  `01 FE 00 01 00000000` (id 254, sort 0). The lamp answers on `…42` with
  `00 <revision u32 LE> <sceneId> <sortIndex>`, e.g. `00 065E0200 06 4B`:
  revision 0x025E06, new scene id 6, sort index 0x4B. It also notifies the
  new id on `…44` (the lamp switches to the new scene).
* **Rename / edit:** the app writes the same 178-byte record again with the
  real id and sort index in the header and the changed fields (only the
  name for a rename).
* **Delete:** the app first selects another scene on `…44`, then writes the
  8-byte header with wireType 0: `01 06 4B 00 00000000`.
* Every write bumps the revision by one.
* Uplight segment example: `A2 701F 92` is saturation 0xA2, hue 0x1F70
  (44°, orange), brightness 0x92. With saturation 0 the u16 is a colour
  temperature: `00 A00F FF` is 4000 K.

### Sync (reading scenes)

Write to `…42`:

```
07 <known revision u32 LE> <database id, 4 bytes> 00 01
```

The app sends the last revision it knows and the database id from the
previous answer (`D2 B2 21 D0` on the test lamp). The lamp answers with
notifications on `…41` (the 36-byte settings record, 12 bytes per
notification, ending with a single `FF`), then on `…43` the records of every
scene slot changed after that revision, also split into 12-byte
notifications, and finally an indication on `…42`:

```
00 <revision u32 LE> 01 <records sent> <selected scene> 9C 00 <database id> 00 00
```

(Seen: `01 00 02` with no scene records and scene 2 selected, `01 01 04`
with one record, `01 14 04` with all 20.)

With revision `00000000` the lamp sends every slot: 20 slots (ids 0–19) on
the test lamp, 1810 bytes in total, empty slots as 8-byte wireType 0
records. The record size follows from the wireType byte, so the stream can
be split without separators. What a Sync with a different or unknown
database id does is not tested.

## Security

* "Protected" mode rejects commands without the security code. Unlock: write
  `00 <u16 PIN>` to `…46`. Remote commands carry the PIN with flag `0x40`.
* Bonding is not required by default. The app has a "Force bonding" option
  for lamps that loop between Offline and Synchronizing, so some lamps may
  only deliver notifications after bonding (*guess*).
