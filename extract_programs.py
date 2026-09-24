"""
Build programs.json from the official Hoover Wizard Android app's program database.

The app package (an .apk is a zip file) contains assets/candy_database.sql (a SQLite file despite the name) holding
234 programs across 16 appliance models. Program values live in
parameters.validation, joined through parameters_for_programs.

    python extract_programs.py <unzipped-app>/assets/candy_database.sql

Writes programs.json next to this script, which server.py then serves to the UI.
"""

import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

# Parameters worth surfacing, wire name -> our name.
WANTED = {
    "selector_position": "sel",
    "pr_code": "code",
    "default_temperature": "temp",
    "maximum_temperature": "tempMax",
    "default_spin_speed": "spin",
    "maximum_spin_speed": "spinMax",
    "default_soil_level": "soil",
    "minimum_soil_level": "soilMin",
    "maximum_soil_level": "soilMax",
    "steam": "steam",
    "dry": "dry",
    "default_duration": "duration",
    "available_options": "opts",
    "available_options2": "opts2",
    "program_type": "ptype",
}

PREFIXES = [
    "DUAL_WM_WD_PROGRAM_NAME_", "DUAL_WM_WD_", "WA_PROG_", "DW_PROG_",
    "DW_WIFI_PROGRAM_NAME_", "DW_WIFI_", "OV_PROG_", "NFC_PROGRAM_NAME_",
    "NFC_TD_PROGRAM_NAME_", "NFC_TD_", "NFC_DW_", "NFC_CARE_", "NFC_",
    "PROGRAM_NAME_",
]


def prettify(name):
    """DUAL_WM_WD_PROGRAM_NAME_RESISTANT_COTTONS -> Resistant Cottons"""
    s = name or ""
    for p in PREFIXES:
        if s.startswith(p):
            s = s[len(p):]
            break
    s = s.replace("_", " ").strip().title()
    s = re.sub(r"\b(\d+)\s*C\b", r"\1°C", s)          # 20 C -> 20°C
    s = re.sub(r"\bMin\b", "min", s)
    return s or name


# Longest-match wins, so DUAL_WM_WD beats DUAL_WM.
FAMILIES = [
    ("DUAL_WM_WD",   "Washer-dryer (dual tech)"),
    ("WA_PROG",      "Washing machine (Wi-Fi)"),
    ("DW_WIFI",      "Dishwasher (Wi-Fi)"),
    ("DW_PROG",      "Dishwasher"),
    ("OV_PROG",      "Oven"),
    ("NFC_TD",       "Tumble dryer (NFC)"),
    ("NFC_DW",       "Dishwasher (NFC)"),
    ("NFC_CARE",     "Care cycle (NFC)"),
    ("NFC_PROGRAM",  "Washing machine (NFC)"),
    ("NFC",          "NFC"),
    ("OFF",          "Off"),
]
FAMILY_LABELS = dict(FAMILIES)


def family_of(name):
    s = name or ""
    for key, _ in sorted(FAMILIES, key=lambda f: -len(f[0])):
        if s.startswith(key):
            return key
    return "OTHER"


# 255 is the firmware's "not applicable" sentinel for temperature and spin.
NA = 255


def num(v):
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return None if n == NA else n


def main(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row

    # program serial -> {param name: value}
    params = {}
    for r in con.execute("""select pfp.serial_program sp, pa.name n, pa.validation v
                            from parameters_for_programs pfp
                            join parameters pa on pa.serial = pfp.serial_parameter"""):
        params.setdefault(r["sp"], {})[r["n"]] = r["v"]

    # program serial -> [appliance ids]
    appliances = {}
    for r in con.execute("select serial_program, id_appliance, program_order "
                         "from programs_for_appliances"):
        appliances.setdefault(r["serial_program"], []).append(
            (r["id_appliance"], r["program_order"]))

    programs = []
    for r in con.execute("select serial, name, position from programs order by serial"):
        raw = params.get(r["serial"], {})
        if not raw:
            continue
        p = {
            "serial": r["serial"],
            "key": r["name"],
            "label": prettify(r["name"]),
            "family": family_of(r["name"]),
            "models": [a for a, _ in appliances.get(r["serial"], [])],
        }
        for wire, ours in WANTED.items():
            v = num(raw.get(wire))
            if v is not None:
                p[ours] = v
        # spin goes on the wire as rpm/100
        if p.get("spin"):
            p["spinWire"] = p["spin"] // 100
        programs.append(p)

    # group by appliance model, keeping the app's own ordering
    models = {}
    for p in programs:
        for mid in p["models"]:
            models.setdefault(mid, []).append(p["serial"])
    # configured_appliances is the app's demo seed, and it is the only place the
    # model UUIDs are given human names ("AWDPD 4138LH/1-S").
    names = {}
    try:
        for r in con.execute("select id, model, type, load_capacity "
                             "from configured_appliances"):
            if r["id"]:
                names[r["id"]] = {"model": r["model"], "type": r["type"],
                                  "load": r["load_capacity"]}
    except sqlite3.Error:
        pass

    model_list = []
    for mid, serials in models.items():
        fams = {pp["family"] for pp in programs if pp["serial"] in serials}
        info = names.get(mid, {})
        model_list.append({
            "id": mid,
            "model": info.get("model"),
            "type": info.get("type"),
            "programs": sorted(serials),
            "families": sorted(fams),
            "count": len(serials),
        })
    model_list.sort(key=lambda m: -m["count"])

    out = {
        "source": os.path.basename(db_path),
        "programs": programs,
        "models": model_list,
        "families": sorted({p["family"] for p in programs}),
        "familyLabels": FAMILY_LABELS,
    }
    dest = os.path.join(HERE, "programs.json")
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, ensure_ascii=False)

    print("wrote %s" % dest)
    print("  %d programs, %d models, families: %s"
          % (len(programs), len(model_list), ", ".join(out["families"])))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python extract_programs.py <candy_database.sql>")
    main(sys.argv[1])
