# candy-hoover-local

Local control for Candy / Hoover **simply-Fi** washing machines (and, for reading, other
simply-Fi appliances). A small web app that talks **directly to the appliance on your
LAN**: no cloud, no account, no Candy/Hoover servers. Python standard library only, so
there's nothing to install.

- A graphical **cycle picker**: tap a cycle, set temperature, spin, soil, extras and delay,
  then start it. While a cycle runs you get a live countdown and finish time.
- An **advanced view**: network scan, raw status, raw commands, and dial learning.
- Works with appliances that answer in plaintext or in their XOR "encrypted" mode. The key
  is worked out from a single reply, so you never need to pull it off your phone.

See [PROTOCOL.md](PROTOCOL.md) for the local API itself.

> Not affiliated with or endorsed by Candy, Hoover or Haier. Names are used only to say
> which appliances this works with.

## Quick start

```bash
python extract_programs.py path/to/assets/candy_database.sql   # once, see below
python server.py
```

Then open <http://127.0.0.1:8099>.

On first run, open **Advanced view → Scan network** to find the appliance, click it, and
press **Save as default** so every page opens straight onto it.

> **Open that URL, not the HTML file.** `index.html` on its own has no backend, so every
> request fails with "Failed to fetch". The page detects this and tells you.

### The program list (`programs.json`)

The cycle names, temperatures, spin speeds and durations come from the program database
that ships inside the official **Hoover Wizard** Android app. It isn't included here —
build it from your own copy of the app:

1. Download the Hoover Wizard `.apk` (it's an ordinary zip file) and unzip it.
2. Run `python extract_programs.py <unzipped>/assets/candy_database.sql`.

That writes `programs.json` beside `server.py`: about 234 programs across 16 models.
Without it, status and raw commands still work, but there's no cycle list.

### Configuration

| Variable | Default | |
|---|---|---|
| `WASH_BIND` | `127.0.0.1` | Address to listen on. |
| `WASH_PORT` | `8099` | Port to listen on. |

The app writes `settings.json` (default appliance) and `profiles.json` (per-appliance
model choice, button and cycle names) next to itself, so its folder must be writable.

## Working out which model you have

You don't need to know your model number. On connect the app fingerprints the machine
from two things it reports itself:

- **`/http-getStatistics.json` returns one `ProgramN` counter per dial position**, which
  gives the machine's program count. That alone is often decisive.
- **The live `PrCode`** must exist in a candidate model's program set.

Candidates are scored and the best is picked automatically, with its reasoning shown.
Override it from the dropdown; your choice is remembered in `profiles.json`.

### When your model isn't in the database

The database covers 16 representative models, so most real machines are matched to the
closest *program set*, not identified exactly. Two things put that right:

- **Rename a cycle.** Program codes are shared between models, but the names aren't
  always the same. If the picker says "Resistant Cottons" while your panel says
  "Eco Cottons", tap the cycle and press **Rename**. Your name is used everywhere from
  then on.
- **Learn my dial** (advanced view). Turn the knob one position at a time and press
  Capture. The machine reports its own program code at each stop, which builds a map of
  your actual machine.

### Options (Prewash, Zoom, extra rinse, …)

Options ride on a 9-bit mask (`Opt1`–`Opt9`, sent as `&OptMsk=<int>`). Each program
declares which bits it accepts, so the picker only offers the ones that cycle supports.

Which bit is which option varies by model and isn't in the database. Set an option on
the machine's own panel, then press **Name a button** in the cycle picker: it shows which
`OptN` is on and lets you label it.

### Machines that change IP address

If your router hands the appliance a different address now and then, point the second
address at the first in `profiles.json` so both share one profile:

```json
{
 "192.168.1.50": {"model": "…", "names": {"65": "Eco Cottons"}},
 "192.168.1.51": {"same_as": "192.168.1.50"}
}
```

Better still, give the appliance a DHCP reservation on your router.

## If the scan finds nothing

The appliance doesn't have to be on the same subnet, only reachable. Leave the subnet box
blank to sweep every subnet this machine is on *plus* the one the Appliance IP field points
at, or type one or more subnets (comma-separated) to sweep exactly those.

These machines sleep their Wi-Fi when idle. If it isn't answering, press a button on the
appliance to wake it.

## Why a local server instead of a pure browser app

The appliance sends no CORS headers and speaks plain HTTP, so a page in your browser can't
call it directly. `server.py` is the thin proxy that makes it work, and it only ever talks
to your LAN.

## Try it without an appliance

```bash
python mock_appliance.py --encrypt
```

Then open `http://127.0.0.1:8099/?ip=127.0.0.1:8080`. The mock picks a random key and
never tells the UI, so you can watch it get worked out anyway. Start a cycle on it and the
countdown runs for real; `MOCK_CYCLE_SECONDS` sets how long.

## Scripting it

```python
import candy_protocol as cp

data, info = cp.read_status("192.168.1.50")
print(info["type"], data)

cp.send_command("192.168.1.50", "Write=1&StSt=0&DelMd=0&PrNm=2",
                key=info.get("recovered_key", ""))
```

## Safety

**The appliance has no authentication, and neither does this app.** Anything that can
reach either one can read the machine's state and start a cycle. Keep the server bound to
`127.0.0.1`, or to your LAN at most. If you want it reachable from outside, put it behind
something that does real authentication (for example Cloudflare Access or Tailscale).
Never expose it straight to the internet.

These are real machines, and `StSt=1` starts one. The UI asks for confirmation before
sending anything that does. Program numbers and option masks vary by model, so check the
dial number against your machine before starting a cycle.

## Files

| File | What it is |
|---|---|
| `candy_protocol.py` | The protocol: cipher, key recovery, read/write. Import it for your own automation. |
| `server.py` | Local HTTP server: serves the UI and proxies to the appliance. |
| `cycles.html` | The cycle picker (default view). |
| `index.html` | Advanced view: scan, raw status, raw commands, dial learning. |
| `extract_programs.py` | Builds `programs.json` from the app's program database. |
| `mock_appliance.py` | A fake appliance for testing without hardware. |

## Credits

Status enums cross-checked against
[ofalvai/home-assistant-candy](https://github.com/ofalvai/home-assistant-candy), an
independent, read-only Home Assistant integration for the same local API.
