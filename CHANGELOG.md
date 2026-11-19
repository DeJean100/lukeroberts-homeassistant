# Changelog

## 0.3.0-dejean.6 (2026-11-19, fork DeJean100)

- **Scene editor card** (`custom:luke-roberts-scene-card`), shipped with the
  integration and loaded automatically: edit a scene on a picture of the
  lamp (uplight segments and downlight spots), with *Spread* to blend a
  change softly, a live preview on the lamp, undo, save and save as new.
  Always full width in sections dashboards; preview in the card picker.
- Websocket commands for the card: `luke_roberts/scenes`, `preview`,
  `end_preview`, `save_scene`.
- **`luke_roberts.test_bluetooth_link`**: read-only measurement of how much
  of a full scene read arrives through a Bluetooth proxy, and where.
- Fix: with a power switch, the lamp stayed *unavailable* when Home
  Assistant started while its power was cut, so it could not be switched
  on from Home Assistant.
- Saving a scene through a lossy Bluetooth proxy: the check afterwards
  accepts damaged copies that fit the written scene (error before, even
  though the lamp had stored it). A created scene is marked as created
  straight away.

## 0.3.0-dejean.5 (2026-11-19, fork DeJean100)

- **Manage scenes in the Configure dialog**: a menu (Connection / Manage
  scenes) for lamps set up over Bluetooth. Pick a scene, then edit (colour
  picker, sliders), rename, duplicate, restore or delete one Home Assistant
  created. No YAML needed.
- **`luke_roberts.edit_scene`**: change the downlight colour temperature and
  brightness, the uplight colour (or white) and the uplight brightness of a
  static scene. Brightness keeps the scene's pattern. Backup first, read
  back afterwards; nothing is written if nothing changes.
- **`luke_roberts.preview_scene`**: show a static scene, optionally with
  changed colours and brightness, on the lamp without saving it (same fields
  as `edit_scene`; selecting a scene ends the preview). Format captured from
  the app.
- Reading changed scenes through a Bluetooth proxy now repairs damaged
  copies like the full read does; checking a written scene no longer fails
  after 10 lossy attempts (seen on the lamp: 3 s instead of an error).

## 0.3.0-dejean.4 (2026-11-19, fork DeJean100)

- **Scene preview, new look.** The uplight (indirect light) is drawn as a soft
  aura of 16 colour blobs around a dark lamp disc that shows the downlight
  spots; the aura no longer reaches into the disc. The picture is round with
  transparent corners, and dim scenes are drawn perceptually so they stay
  visible. The entity icon follows the scene straight away (the picture URL
  carries a version).

## 0.3.0-dejean.3 (2026-11-18, fork DeJean100)

- **Scene preview** image entity: the active scene drawn from its scene
  record (uplight ring and downlight spots). The scene records are kept in
  memory and updated with the changes at startup and with *Refresh scenes*.
- **`luke_roberts.duplicate_scene`** copies a static scene into a free slot
  under a new name (the lamp switches to it, as in the app) and returns the
  new id. **`luke_roberts.delete_scene`** deletes only scenes Home Assistant
  created that way. Both make a backup first and check the result.

## 0.3.0-dejean.2 (2026-11-18, fork DeJean100)

- **Scene backup, rename and restore.** New actions
  `luke_roberts.backup_scenes`, `rename_scene` and `restore_scene` read and
  write the lamp's scene records like the Luke Roberts app (Sync command and
  scene record characteristic `…43`, little endian). Every write is preceded
  by a backup of all scenes in Home Assistant's storage and checked by
  reading the scene back. Off and dynamic scenes are never written. Verified
  on a Model F (firmware 1.2.2): a rename changes only the name, leaves the
  other scenes and the active scene alone, and a restore is byte for byte.
- Scenes are read like the app does: a copy is kept in Home Assistant and
  only the scenes changed since then are asked for (about 0.2 s when nothing
  changed). This makes it work through Bluetooth proxies that lose
  notifications: the first full read collects intact scenes over several
  attempts and rebuilds damaged ones from two copies when that is
  unambiguous, and a damaged read is never taken over. Through an ESPHome
  proxy: first read about 25 s, rename or restore under 3 s.

## 0.3.0-dejean.1 (2026-11-18, fork DeJean100)

Fork release with PR #16, PR #17 and the power switch option, which are
not merged upstream yet. Same domain as the original, so it replaces it.

- **Power switch option.** A lamp on a switched circuit (KNX or Shelly
  actuator, smart plug, relay) can name that light or switch entity as
  *Power switch* in its options. On/off then switches the power, the light's
  on/off state follows that entity, and brightness, colour temperature and
  scenes still go over Bluetooth: after power-up Home Assistant waits for the
  lamp (about 2 s) before applying them, and reads the lamp's state whenever
  the power goes from off to on, so the scene picked by the lamp's own
  power-on and click detection settings is shown. If Home Assistant turned
  the light on and a very short cut left the lamp in its Off scene, *Default*
  is selected. Tested with a KNX actuator on a Model F.
- **Brightness, colour temperature and on/off read from the lamp.** After
  every Bluetooth command, and when the lamp details are read at startup, the
  integration sends the undocumented command `09` (one byte, no `A0` prefix)
  and takes over the answer `00 88 F4 18 71 KK KK BB FF`: colour temperature
  in Kelvin (little endian), brightness in percent and `01` on / `00` off.
  It comes from
  [martgras/LukeRobertsControl](https://github.com/martgras/LukeRobertsControl)
  and was confirmed on a Model F with firmware 1.2.2: the percentage matches
  Modify Brightness (`03`, 30 → 30, 75 → 75, relative −20 → 55) and the Kelvin
  value matches `04`. Only that exact answer is accepted; firmware that
  answers differently, or not at all, keeps the assumed state and is not
  asked again until Home Assistant restarts.
- A selected scene now shows its **own brightness** (for example 42 %)
  instead of the assumed 100 %, and Brighter / Dimmer show where the lamp
  actually ended up.
- **Active scene read from the lamp.** On every Bluetooth connection the
  integration reads the scene characteristic `44092844-…` (one byte, the
  active scene id) and subscribes to its notifications, so the scene select,
  the light's effect and on/off follow scenes chosen in the app or with click
  detection. This characteristic is not in the Lamp Control API v1.3
  document; it was confirmed on a Model F with firmware 1.2.2 (reads `01`
  while scene 1 "Indirect" was active). Firmware without it keeps the
  assumed state as before.
- The lamp notifies the new scene *before* it answers the command, so after
  *Next brighter/dimmer scene* the select now shows the scene the lamp
  actually stepped to instead of going blank, and *Default* shows the real
  scene behind it.

## 0.2.6

- Brand images: the neutral lamp drawing is replaced by the Luke Roberts logo
  (icon and logo, with dark mode variants) in `brand/`.

## 0.2.5

- **Fixed Brighter / Dimmer** (issue #13). The Lamp Control API v1.3 says
  Relative Brightness (opcode `08`) multiplies the brightness by `PP/100`,
  so the buttons sent 120 and 80. The lamp actually reads the byte as a
  signed change in percentage points, like the Luke Roberts app 2.4.0 does,
  so both buttons brightened. They now send +20 and −20. Confirmed on a real
  lamp with the test select from the 0.2.5b1 pre-release; that select is
  removed again and cleaned up from the entity registry automatically.
- New [docs/dashboard.md](docs/dashboard.md) with a full dashboard and a
  diagnostics card.
- Release workflow: tags like `v1.2.3b1` or `v1.2.3rc1` are published as
  GitHub pre-releases, which HACS only offers with the repository's
  pre-release switch turned on.

## 0.2.4

- **Removed** the experimental actions `luke_roberts.save_scene` and
  `luke_roberts.delete_scene`. The scene record format is not part of the
  published Lamp Control API v1.3 and was never confirmed on a real lamp,
  and both actions overwrite or erase scenes in the lamp without a way to
  undo it from Home Assistant. They come back once the format is verified
  (for example by Luke Roberts or a capture of the app writing a scene).
  Automations or scripts that call these actions need to be changed.
  Selecting scenes, the scene list and `luke_roberts.immediate_light` are
  unchanged.

## 0.2.3

No changes to the integration itself. Release for the submission to the
HACS default repository.

- Validate workflow: the HACS check runs without ignores (the brands check
  passes with the bundled `brand/icon.png`), and both HACS and hassfest
  validation also run daily.

## 0.2.2

- Firmware check: a lamp that is up to date showed no newest version, because
  the firmware server answers `{}` then. It now shows "up to date".
- Firmware check runs right after start and every 6 hours (was 12). After a
  restart it uses the firmware version stored on the device, so the lamp
  does not have to be reachable over Bluetooth first.
- Failed firmware checks are logged as a warning instead of only at debug
  level.
- Lamp details (model, firmware, scenes) are read again when the first
  attempt timed out, as soon as a connection succeeds, and when the lamp shows
  up again after a gap (for example a firmware update with the app). Before,
  they were only read once per start.

## 0.2.1

- **Scenes from Home Assistant** (experimental): `luke_roberts.save_scene`
  creates or overwrites a scene in the lamp, `luke_roberts.delete_scene`
  removes one. Record format from the Luke Roberts app 2.4.0.
- **Firmware entity**: installed firmware, and with a firmware check key in
  the lamp options the newest version and release notes from the Luke Roberts
  firmware server
- Device page shows the firmware version (2A28) instead of the bootloader
  revision (2A26)
- Diagnostics list what the Bluetooth adapters and proxies last saw of the
  lamp; setup logs a hint when no connectable scanner sees it (for example
  while an ESPHome gateway or the phone app holds the connection)
- Status `0xFD` (locked) is treated like a security code, `0x85` reports a
  full scene memory

## 0.2.0

- **Luke Roberts account**: add the API key once. Bluetooth lamps are linked
  to their cloud lamp automatically (serial number or name) and the other
  lamps of the account are offered as discovered devices. This replaces the
  "cloud only" setup per lamp; existing entries keep working.
- A rejected account key asks for a new key once on the account instead of
  on every lamp
- Discovery also matches lamps by advertised name (`LRBT…`, `Luvo…`), for
  proxies that do not see the service UUID in passive scans
- Integration icon shipped in `brand/` (Home Assistant 2026.3+)
- Ko-fi / Buy Me a Coffee links, issue templates, Dependabot for actions

## 0.1.0 (first preview)

First public preview. Expect bugs and breaking changes before 1.0.

- Local control over Bluetooth LE through Home Assistant adapters and
  Bluetooth proxies (ESPHome, Shelly, ...), with automatic discovery
- Optional Luke Roberts cloud fallback via personal API key, four
  connection modes
- Light entity with brightness, colour temperature and scenes as effects
- Scene select with the names stored in the lamp
- Buttons for next brighter/dimmer scene, brighter/dimmer, refresh scenes,
  ping
- `luke_roberts.immediate_light` action for uplight colour and downlight
- Diagnostics download, English and German translations
