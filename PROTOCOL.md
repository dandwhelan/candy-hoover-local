# simply-Fi local API

Candy / Hoover **simply-Fi** appliances run a small HTTP server on **port 80** and can
be read and controlled directly over the LAN. The official app uses the same path, and
the cloud isn't involved in local control.

This is the older simply-Fi platform, not the newer cloud-only **hOn** platform.

## Endpoints

| Purpose | Request |
|---|---|
| Read status (plain) | `GET http://<ip>/http-read.json?encrypted=0` |
| Read status (encrypted) | `GET http://<ip>/http-read.json?encrypted=1` |
| Write command (plain) | `GET http://<ip>/http-write.json?encrypted=0&<params>` |
| Write command (encrypted) | `GET http://<ip>/http-write.json?encrypted=1&data=<hex>` |
| Statistics | `GET http://<ip>/http-prepareStatistics.json?encrypted=<0\|1>`, then `http-getStatistics.json` |

Every call is a plain `GET`. **There is no authentication, token or session**: anything
that can reach port 80 can read and command the appliance.

## Encryption

A **repeating-key XOR** over the raw bytes, hex-encoded in uppercase. It is symmetric,
so the same function encrypts and decrypts. The key is a random 16-character alphanumeric
string, set when the appliance is paired and stored on both the appliance and the phone.

Two things worth knowing:

1. **`encrypted=0` is a supported mode on the same firmware.** Many units answer
   plaintext happily, so try that first.
2. **The key can be worked out from a single encrypted reply.** Every washer status
   document begins `{"statusLavatrice":{"`, which is longer than the 16-byte key. XOR
   that known prefix against the ciphertext and the whole key falls out.
   `candy_protocol.recover_key()` does this, allowing for the whitespace variations real
   units add. It's a property of the appliance's design and worth bearing in mind when
   judging how much the "encryption" protects.

During pairing only, two derived keys are used instead: the first 8 characters of the
cleaned SSID doubled, and the last 8 characters doubled.

## Discovery

Appliances can be found two ways:

- **UDP announce**: the appliance broadcasts a packet with its MAC address, IP and key.
- **LAN sweep**: try `http://<ip>/http-read.json` on each host.

`server.py` uses the sweep, with a quick TCP/80 check first to keep it fast.

## Status documents

The root key names the appliance family:

| Root key | Appliance |
|---|---|
| `statusLavatrice` | washing machine |
| `statusTD` | tumble dryer |
| `statusDWash` | dishwasher |
| `statusForno` | oven |
| `statusHob` | hob |
| `statusRX` | fridge |

Common washer/dryer fields: `WiFiStatus`, `Err`, `MachMd` (machine state), `Pr`, `PrPh`
(program phase), `PrCode`, `Temp`, `SpinSp`, `Steam`, `DryT`, `DelVal` (delay), `RemTime`,
`Opt1`–`Opt9`, `CheckUpState`.

Seen on a real washer as well: `SLevel` (soil level), `RecipeId`, `Lang`, `FillR` (drum
fill), `DisTestOn` / `DisTestRes` (self-test).

Auto-dose washers also report:

| Field | Meaning |
|---|---|
| `Det`, `Soft` | auto-dose of detergent / softener is on |
| `DetWarn`, `SoftWarn` | tank empty |
| `DetPreW`, `SoftPreW` | tank on reserve; once `DPrgCnt` / `SPrgCnt` reaches 3 or more, treat it as empty |
| `WaterHard` | 1 soft (0–12 °f), 2 medium (13–30 °f), 3 hard (over 30 °f) |
| `rED` | extra dose for the running cycle |

`Lang` is the display language: 0 it, 1 en, 2 fr, 3 nl, 4 pt, 5 es, 6 de, 7 bg, 8 cs,
9 sk, 10 hr, 11 el, 12 pl, 13 sr, 14 sl, 15 ro, 16 ru, 17 uk, 18 hu, 19 da, 20 fi, 21 no,
22 sv, 23 tr, 24 zh, 25 ar.

`CheckUpState` is 1 while the self-check runs and 2 when it's done; `DisTestRes` 1 is OK,
anything else a fault. `T0W TIW T0R numF unbF unbC NtcW NtcD motS APSoff APSfreq chartL`
are diagnostics with no known meaning.

### Enums

`MachMd`, machine state:

| | | | |
|---|---|---|---|
| 1 Idle | 2 Running | 3 Paused | 4 Delayed start selected |
| 5 Delayed start programmed | 6 Error | 7 Finished | 8 Finished |

`PrPh`, wash program phase:

| | | | |
|---|---|---|---|
| 0 Stopped | 1 Pre-wash | 2 Wash | 3 Rinse |
| 4 Last rinse | 5 End | 6 Drying | 7 Error |
| 8 Steam | 9 Spin (good night) | 10 Spin | |

`DryT`, dry level: 0 No dry, 1 Iron, 2 Hang, 3 Store, 4 Bone. The AWMPD 410LH8/1-S notes
give a different scale for that washer-dryer: 0 none, 1 extra, 2 iron, 3 cupboard,
5 / 6 / 7 / 8 = 120 / 90 / 60 / 30 min, 9 wool. Firmware likely differs here, so check
yours.

`Steam` (AWMPD 410LH8/1-S): 1 cotton, 2 mixed, 3 refresh, 5 on.

`Err`, washer error code, in **decimal**:

| Code | Meaning |
|---|---|
| 1, 7 | door lock |
| 2 | water inlet |
| 3 | drain |
| 4 | water level (often excess foam) |
| 6 | unbalanced load (you can fix this yourself) |
| 8 | drum rotation |
| 12 | drying system |
| 16 | water heating |
| 5, 9, 11, 13, 15, 17 | electronics |

These agree with [ofalvai/home-assistant-candy](https://github.com/ofalvai/home-assistant-candy),
an independent read-only implementation of the same API.

### Units, and the traps

- **`RemTime` is in seconds, not minutes.** A running cycle reporting `3300` has about
  55 minutes left.
- **`SpinSp` is rpm ÷ 100.** `8` means 800 rpm.
- **`FillR` is a percentage** of drum fill.
- **`WiFiStatus` is not link state.** It says whether the panel is in remote mode, and
  the appliance only accepts commands while it reads `1`. If writes are accepted but
  nothing happens, check this first.
- **`Pr` is the dial position, not a program identifier.** Tested on a real washer: the
  lifetime counter that went up at the end of a cycle was the `ProgramN` matching `Pr`.
  Positions are shared across different programs from model to model, so identify
  *what* is running by `PrCode`.

## Commands

**Washer**

```
Write=1&StSt=1
        [&DelVl=<n>]                delayed start, in 30-minute steps
        &PrNm=<dial position>
        &PrCode=<program code>      this is what picks the program
        &PrStr=<program name>
        [&TmpTgt=<°C>]              temperature
        [&SLevTgt=<n>]              soil level
        [&SpdTgt=<rpm/100>]         spin
        &OptMsk1=<int>&OptMsk2=<int>  options, see below
        &Stm=<0|1>                  steam
        [&RecipeId=…] [&StartCheckUp=…]

stop      Write=1&StSt=0&PrNm=<running position>&DelVl=0    (tested on a running cycle; should also cancel a delayed start)
reset     Write=1&StSt=0&PrNm=2
check-up  Write=1&CheckUpState=1
```

More washer commands, from the AWMPD 410LH8/1-S notes. *(untested)* means not yet
verified on hardware.

| Command | Parameters |
|---|---|
| Pause | `Pa=1`, with no `Write=1` *(untested)* |
| Resume | `Pa=0`, with no `Write=1` (confirmed on an AWDPD4138LHR/1) |
| End the Good Night hold (`PrPh=9`) and finish with a spin | `Write=1&RestartGN=1` *(untested)* |
| Auto-dose on / off | `SetDet=<0\|1>&SetSoft=<0\|1>`, always both *(untested)* |
| Water hardness | `SetHard=<1-3>`, sent before `SetDet` / `SetSoft` *(untested)* |
| Self-check (empty drum) | `CheckUpState=1`, then close with `Write=1&StSt=0&PrNm=11` *(untested)* |

On the start command, `ED=1` asks for the "intensive wash" extra dose (auto-dose models,
programs that offer Prewash); while it's set, other options are off *(untested)*. The
same notes also send `&Lang=<current Lang>&Stm=0&Dry=0&DispTestOn=1`. Leave out `TmpTgt`
and `SpdTgt` when the program has none (255), and cap both at the program's maximum.

This is the start command the official app sends, as seen in packet captures by
[bigmoby/home-assistant-candy](https://github.com/bigmoby/home-assistant-candy).

- **Send `PrCode`.** Tested on a real washer: `PrNm=1` with no `PrCode` was accepted
  (`SUCCESS`) but the machine ran Drain + Spin, which it had last been set to. Learn each
  position's code from the status (`Pr` and `PrCode` always describe the same program).
- **Spin goes out as rpm ÷ 100**, the same as it's reported: 1400 rpm is sent as `14`.
  Temperature is plain °C.
- **`255` means "not applicable"** for temperature and spin in the program database
  (rinse and drain programs use it).
- An earlier version of this file had `Pa=0&Sel=0`, `TmpDf`, `SpdDef`, `DelMd=1` and a
  delay in minutes on the start command. The official app sends none of those.

**Dishwasher**: `OpzProg`, `OptMsk1`, `OptMsk2`, `OpenDoorOpt=7`, `Reset=1`, `DelayStart`.

**Oven**: `Program`, `StartStop`, `TempSet`, `TimeProgr`, `DelayStart`, `PrL`, `StL`,
`TL`, `TmpLow`, `RecipeId`, `RecipeStep`, `GetStats=1`.

**Vacuum**: only `LockSt=0` / `LockSt=1` and `TimeSync=<yyyy/MM/dd_HH:mm>`. It's
read-only telemetry plus a child lock; there's no drive or clean command.

### Options (Prewash, Zoom, extra rinse, …)

Options are a **9-bit mask** built from the status fields, most significant first:

```
mask = int(Opt9 Opt8 Opt7 Opt6 Opt5 Opt4 Opt3 Opt2 Opt1, base 2)
```

So **bit 0 (value 1) is `Opt1` … bit 8 (value 256) is `Opt9`**, and it's sent back as
`&OptMsk1=<int>`, with `&OptMsk2=<int>` for the second mask (`0` on most machines).

Which options a cycle accepts is its `available_options` value in the program database,
using the same bit layout. On AWMPD610LH8B-80, for example:

| Program | `available_options` | binary | allowed |
|---|---|---|---|
| Resistant Cottons, Eco Cottons | 251 | `011111011` | Opt1, Opt2, Opt4–Opt8 |
| Eco 20, Intensive 40 | 248 | `011111000` | Opt4–Opt8 |
| Delicates, Wool | 156 | `010011100` | Opt3, Opt4, Opt5, Opt8 |
| Baby 60 | 186 | `010111010` | Opt2, Opt4, Opt5, Opt6, Opt8 |
| Hygiene 60 | 136 | `010001000` | Opt4, Opt8 |
| Rinse, Drain Spin, all Rapids, Jeans, Maintenance | 0 | — | none |

**The program database doesn't say which bit is which option.** It has the names
(Aquaplus, Anticrease, Extra dose, Good night, Hygiene, Prewash, Rinse +1/2/3, Zoom,
Steam, Softener, Night & day, Refresh) but never ties one to a bit. On a Hoover
**AWMPD 410LH8/1-S** (interface `red_devil_axi_ad`) the bits are:

| Value | Field | Option |
|---|---|---|
| 1 | `Opt1` | Prewash |
| 2 | `Opt2` | Hygiene |
| 4 | `Opt3` | Anti-crease |
| 8 | `Opt4` | Good Night |
| 16 / 32 / 64 | `Opt5`–`Opt7` | Extra rinse +1 / +2 / +3 |
| 128 | `Opt8` | AquaPlus |
| 256 | `Opt9` | Zoom |

Two groups are **mutually exclusive**: {Prewash, Hygiene, Good Night} (1, 2, 8) and the
extra rinses (16, 32, 64). Send at most one from each, masked against the program's
`available_options`. That fits the AWMPD610LH8B-80 table above (Wool takes Anti-crease,
Good Night, Rinse +1 and AquaPlus, but not Prewash).

The cycle picker uses these names by default and enforces both groups. Other models may
map bits differently: set the option on the machine's own panel, see which `OptN` flips,
and use **Name a button** to relabel it.

### Value scales

- **Soil level (`SLevTgt`, reported as `SLevel`) is 1–3** on the Wi-Fi washers, and each
  program has its own allowed range. **`0` means the program has no soil setting**, so
  leave `SLevTgt` out for those.
- **Temperature** is one of `0, 20, 30, 40, 60, 90` (°C, `0` = cold), capped per program.

## The program database

The official app carries a SQLite file, `assets/candy_database.sql` despite the name,
holding **234 programs across 16 appliance models**. Values live in the **`validation`**
column of `parameters`, joined to a program through `parameters_for_programs`:

```sql
select pa.name, pa.validation
from parameters_for_programs pfp
join parameters pa on pa.serial = pfp.serial_parameter
where pfp.serial_program = ?
```

Per-program parameters include `selector_position`, `pr_code`, `default_temperature`,
`maximum_temperature`, `default_spin_speed`, `maximum_spin_speed`,
`default/minimum/maximum_soil_level`, `steam`, `dry`, `default_duration`,
`available_options` and `available_options2`.

Program families by name prefix: `DUAL_WM_WD` (washer-dryer), `WA_PROG` (Wi-Fi washer),
`DW_PROG` / `DW_WIFI` (dishwasher), `OV_PROG` (oven), and `NFC_*` for NFC-tagged cycles.

`extract_programs.py` turns this into `programs.json`.

### Lifetime counters

`http-getStatistics.json` returns one `ProgramN` counter per dial position, plus
`Temp0to30`, `Temp40` and `Temp60to90` (heating runs, so they add up to more than the
cycle count) and dryer counters. Send `http-prepareStatistics.json` first and wait about
two seconds, or every counter reads `0`. A counter goes up when its cycle finishes.

SmartPro, XCare and HWash300 models use `http-write.json?GetStat=1` instead.

**Program codes are shared between models, but names aren't always.** A machine that
isn't one of the 16 can report a code that the database names differently from its own
panel, which is why the app lets you rename cycles.

## Recipes ("downloadable" programs)

*(untested)* Nothing is stored on the washer. A recipe is an ordinary start of a base
program with fixed values plus `RecipeId=D_<position>`, and the washer echoes `RecipeId`
back in its status. The list comes from the cloud:
`GET /api/v1/wm_wd_programs.json?interface_type=red_devil`, with fields `position`,
`parent`, `temperature`, `spin_speed`, `soil_level`, `steam`, `dry` and `options`
(`options` are the selectable ones, not presets; spin `MAX` means the program maximum).

`parent` names the base program: 1 cottons, 2 mixed / synthetics, 3 wool, 4 delicates,
5 / 6 / 12 rapid 14 / 30 / 44, 7 / 9 44–59 min cycles, 8 rinse, 10 coloured cottons,
11 delicates 59, 101–103 drying. Pick the first matching program on the machine. For
example, Shirts is parent 9 at 30 °C and 1000 rpm, sent with `RecipeId=D_104`.

## Pairing

**AP mode.** While connected to the appliance's own access point:

```
GET /http-config.json?encrypted=0&NTW_MODE=<sec>&SSID=<ssid>&PASSWORD=<pw>&ENCRYPT_KEY=<16 chars>
→ {"response":"success"}
```

The encrypted variant uses the last 8 characters of the appliance's AP SSID, twice, as
the key.

**BLE** is for pairing only; there's no status or control over BLE. Service `0000A002-…`
has `C300` (read: Wi-Fi MAC), `C301` (notify: done) and `C302` (write). Appliances
advertise manufacturer ID 741 (Espressif), 1349 or 51712. Connect, enable notifications,
read `C300`, write the packet to `C302`, and wait for the notification:

```
A5 | total length | 16 | "NTW_MODE=..&SSID=..&PASSWORD=..&ENCRYPT_KEY=.." | CRC16 (big-endian)
```

The CRC16 covers everything except itself: init `0xFFFF`, reflected, poly `0x6363`,
final `~crc & 0xFFFF`.

## Sources

The option bits, extra commands, auto-dose fields, error codes, recipes and pairing
come from [rylos's notes](https://gist.github.com/rylos/42e2360258ad07cb777e54bc3f8df642)
on a Hoover AWMPD 410LH8/1-S ([issue #1](https://github.com/dandwhelan/candy-hoover-local/issues/1)).

## Caveats

- Firmware varies. Some units reject `encrypted=0`, and some report only a subset of
  fields.
- These are real appliances. `StSt=1` starts a physical machine.
- Program numbers and option masks differ per model. The strings above are the shapes;
  check the specific numbers against your own machine.
