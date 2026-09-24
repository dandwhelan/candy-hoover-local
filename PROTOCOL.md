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

`DryT`, dry level: 0 No dry, 1 Iron, 2 Hang, 3 Store, 4 Bone.

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
- **`Pr` is not the dial position.** Identify the running program by `PrCode`.

## Commands

**Washer**

```
Write=1&Pa=0&Sel=0&PrNm=<dial position>
        [&DelMd=1&DelVl=<min>]      delayed start
        [&StSt=1]                   start
        [&TmpTgt=<°C>&TmpDf=<°C>]   temperature
        [&SLevTgt=<n>]              soil level
        [&SpdTgt=<rpm/100>&SpdDef=<rpm/100>]
        [&Stm=<0|1>]                steam
        [&OptMsk=<int>]             options, see below
        [&RecipeId=…] [&CheckUpState=…]

stop      Write=1&StSt=0&DelMd=0&PrNm=<dial position>
reset     Write=1&StSt=0&PrNm=2
check-up  Write=1&CheckUpState=1
```

- **`Sel` is always the literal `0`.** It's **`PrNm`** that carries the dial position.
- **Spin goes out as rpm ÷ 100**, the same as it's reported: 1400 rpm is sent as `14`.
  Temperature is plain °C.
- **`255` means "not applicable"** for temperature and spin in the program database
  (rinse and drain programs use it).

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
`&OptMsk=<int>`. Some machines have a second mask, `OptMsk2`.

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

**Which bit is which option varies by model and isn't recorded anywhere.** The option
names exist (Aquaplus, Anticrease, Extra dose, Good night, Hygiene, Prewash, Rinse +1/2/3,
Zoom, Steam, Softener, Night & day, Refresh), but nothing ties a name to a bit. The
reliable way is to set the option on the machine's own panel, see which `OptN` flips, and
note the name. The cycle picker's **Name a button** does exactly that.

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

**Program codes are shared between models, but names aren't always.** A machine that
isn't one of the 16 can report a code that the database names differently from its own
panel, which is why the app lets you rename cycles.

## Caveats

- Firmware varies. Some units reject `encrypted=0`, and some report only a subset of
  fields.
- These are real appliances. `StSt=1` starts a physical machine.
- Program numbers and option masks differ per model. The strings above are the shapes;
  check the specific numbers against your own machine.
