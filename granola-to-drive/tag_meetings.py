#!/usr/bin/env python3
"""
tag_meetings.py — classify every Granola dossier with a primary category and
secondary tags, then inject a `Tags:` line near the top of each dossier file.

Run after build_docs.py. Idempotent (replaces any existing Tags: line).

Primary categories (one per meeting):
  customer-discovery, peer-founder, mentor-advisor, vc-networking,
  recruiting, research-academic, internal-team, factory-tour,
  partnership-bd, grants-legal, industry-talk, other

Secondary tags (zero or more): industries + technical topics + strategy.
"""

import json
import os
import re
from pathlib import Path

ROOT = Path(os.environ.get(
    "GRANOLA_EXPORT_DIR",
    str(Path.home() / "Library/Application Support/Granola/granola_export"),
))
BUILT = ROOT / "built"

# ---------------------------------------------------------------------------
# Per-meeting overrides — keyed by meeting UUID. Values: (primary, [secondary]).
# Only listed when rules below would misclassify. Anything not here falls back
# to keyword rules.
# ---------------------------------------------------------------------------
OVERRIDES: dict[str, tuple[str, list[str]]] = {
    # Add personal corrections here, keyed by Granola meeting UUID:
    # "abcd-1234-...": ("vc-networking", ["fundraising"]),
}

# ---------------------------------------------------------------------------
# Rules — applied in order. First match wins. Each rule:
#   (regex_against_title, primary, secondary_tags)
# Note: secondary tags are *added on top* of any inferred from the body.
# ---------------------------------------------------------------------------
TITLE_RULES: list[tuple[str, str, list[str]]] = [
    # internal-team / coursework
    (r"^Algorithmic Human Robot", "research-academic", ["imitation-learning", "coursework"]),
    (r"\bTeam Meeting\b", "internal-team", []),
    (r"^BEAVR\b", "internal-team", ["teleoperation"]),
    (r"^Co Create.*planning", "internal-team", ["partnership-bd"]),

    # industry-talk (lectures/presentations)
    (r"\bDelve\b.*Marketing", "industry-talk", ["fundraising"]),

    # grants-legal
    (r"\bM2I2\b", "grants-legal", []),
    (r"\bNSF funding\b", "grants-legal", ["fundraising"]),
    (r"\bMIT student legal\b", "grants-legal", ["legal-ip"]),

    # vc-networking
    (r"\bPierce Fellows\b", "vc-networking", ["fundraising"]),
    (r"^Rashad Haque", "vc-networking", ["fundraising"]),
    (r"^Khanh Dang", "vc-networking", ["fundraising"]),
    (r"^Talk with Ted", "vc-networking", ["fundraising", "peer-founder"]),
    (r"^Ted <> Dillon\b", "peer-founder", ["fundraising"]),  # earlier Ted call

    # mentor-advisor
    (r"\bVMS Mentoring\b", "mentor-advisor", []),
    (r"\bSudhir Jain\b|\bSudhir <> Dillon\b", "mentor-advisor", ["fundraising"]),
    (r"\bRam Kumar\b", "mentor-advisor", []),
    (r"\bCalvin Chin\b", "mentor-advisor", ["fundraising"]),
    (r"\bJeff Lipton\b", "mentor-advisor", []),
    (r"\bGuido Jacques\b", "mentor-advisor", ["aerospace-defense"]),
    (r"\bHaden Quinlan\b", "mentor-advisor", ["machining-cnc"]),
    (r"\bRichard Linares\b", "mentor-advisor", ["teleoperation"]),
    (r"^Mike <> Dillon", "mentor-advisor", ["robotics-data", "simulation"]),  # Duality/Mike
    (r"\bManufacturing startup mentorship\b", "mentor-advisor", []),
    (r"\bManufacturing startup pricing models\b", "mentor-advisor", ["fundraising"]),
    (r"\bStartup prototype development and funding\b", "mentor-advisor", ["fundraising"]),
    (r"\bStartup strategy session with Jeff\b", "mentor-advisor", []),

    # peer-founder
    (r"^Bryan Zin", "peer-founder", ["consumer-electronics"]),
    (r"^Onder", "peer-founder", ["manipulation"]),
    (r"^Charles Blanchet", "peer-founder", ["humanoids"]),
    (r"^Karsten\b", "peer-founder", ["robotics-data"]),
    (r"^Steve.*Allium", "peer-founder", ["building-materials"]),
    (r"\bLumafield\b", "peer-founder", ["fundraising", "vision-systems"]),
    (r"^Gustavo Castillo", "peer-founder", ["food-restaurants"]),
    (r"^Ignacio\b", "peer-founder", ["food-restaurants"]),
    (r"^Haoshu Fang", "peer-founder", ["fundraising", "manipulation"]),
    (r"^Maja <> Dillon", "peer-founder", ["humanoids"]),
    (r"^Dillon <> Grably", "peer-founder", ["robotics-data"]),
    (r"^Dillon <> Dominique", "peer-founder", ["teleoperation"]),
    (r"^Dillon <> Alberic", "peer-founder", ["robotics-data", "humanoids"]),
    (r"\bDillon \(LineGuard\)\b", "peer-founder", []),
    (r"^Alexander Schmitz", "peer-founder", ["tactile-sensing"]),
    (r"^Michael Healy", "peer-founder", ["teleoperation", "robotics-data"]),

    # recruiting
    (r"^Davit\b", "recruiting", ["co-founder-search"]),
    (r"^Pedram\b.*Dillon", "recruiting", []),
    (r"^Keivalya\b", "recruiting", ["co-founder-search"]),
    (r"^Dillon <> Axel\b", "recruiting", ["co-founder-search"]),
    (r"\bNathan Meeting\b", "recruiting", ["co-founder-search"]),
    (r"\bRolando Bautista\b", "recruiting", ["co-founder-search"]),
    (r"^Sam Bodmer", "recruiting", ["research-academic"]),
    (r"\bRobotics career exploration\b", "recruiting", ["research-academic"]),
    (r"\bRobotics research and career insights\b", "recruiting", ["research-academic"]),

    # research-academic (researcher peers, classes, lab discussions)
    (r"^Clement Busuttil", "research-academic", ["imitation-learning"]),
    (r"\bRobotics data collection strategies with CCIL\b", "research-academic", ["robotics-data"]),
    (r"\bRobotics data collection and training\b", "research-academic", ["robotics-data"]),
    (r"\bExploring robotics data collection and model development\b", "research-academic", ["robotics-data", "simulation"]),
    (r"\bCo Create first engagement\b", "internal-team", ["partnership-bd"]),

    # partnership-bd
    (r"\bOwen Rapaport\b", "partnership-bd", ["fundraising"]),
    (r"\bAndre & Cocreate\b", "partnership-bd", []),
    (r"\bRosco Vision Pilot Pitch\b", "partnership-bd", ["pilot-active", "automotive"]),
    (r"\bManufacturing automation pilot planning with Kyle", "partnership-bd", ["pilot-active", "automotive"]),

    # factory-tour
    (r"\bSite Visit\b", "factory-tour", ["machining-cnc"]),
    (r"\bProduction Tour\b", "factory-tour", []),
    (r"\bShowroom Tour\b", "factory-tour", []),
    (r"\bMicrofactory Visit\b", "factory-tour", []),
    (r"^FirstBuild\b", "factory-tour", []),
    (r"\bWarehousing and HVAC\b", "factory-tour", []),
    (r"^AL-AN manufacturing\b", "factory-tour", ["machining-cnc"]),

    # other
    (r"^Interview request in Korean", "other", []),
]

# ---------------------------------------------------------------------------
# Body-keyword → secondary tag mappings. Applied in addition to title rules.
# ---------------------------------------------------------------------------
INDUSTRY_KEYWORDS = {
    "aerospace-defense": [
        r"\baerospace\b", r"\bdefense industry\b", r"\bdefence industry\b",
        r"\bnuclear (manufactur|reactor|industry|certified)\b",
        r"\bDARPA\b", r"\bSpace Force\b", r"\bL3 Harris\b",
        r"\bWestinghouse\b", r"\bmissile\b", r"\bmunitions\b",
        r"\bdetonator\b", r"\bjet engine\b", r"\bDOD\b",
    ],
    "automotive": [
        r"\bautomotive\b", r"\bcar manufact", r"\bToyota\b",
        r"\bNissan\b", r"\bStellantis\b",
        r"\bRoscoe\b|\bRosco\b", r"\bVolvo\b", r"\bMutinauti\b",
        r"\bbus mirror\b", r"\bvehicle assembly\b",
    ],
    "medical-pharma": [
        r"\bmedical device", r"\bpharmaceutical\b", r"\bhealthcare manufact",
        r"\bIVD\b", r"\bPiSA\b", r"\bASML\b",  # ASML is semiconductor lithography for chipmakers
        r"\bstent\b", r"\bblister pack",
    ],
    "building-materials": [
        r"\bbuilding material", r"\baerogel\b", r"\bMasonite\b", r"\bMasterBrand\b",
        r"\bKoetter\b", r"\bdoor manufact", r"\bcabinet manufact",
        r"\bglass manufact", r"\bAmrize\b", r"\bAllium\b", r"\brebar\b",
        r"\bOSB\b", r"\bplywood\b",
    ],
    "electronics-ems": [
        r"\bPCBA\b", r"\bEMS\b", r"\belectronics manufact",
        r"\bcontract manufact",
        r"\bVR Industries\b", r"\bSolaria\b", r"\bsurface mount\b",
    ],
    "mining-explosives": [
        r"\bmining industry\b", r"\bexplosives manufact", r"\bDyno Nobel\b",
        r"\bquarry", r"\bcone crusher\b", r"\bjaw crusher\b",
    ],
    "solar-energy": [r"\bsolar (panel|cell|module)\b", r"\bSilfab\b", r"\bphotovoltaic\b"],
    "food-restaurants": [r"\brestaurant automation\b", r"\bkitchen automation\b", r"\bbakery\b", r"\bcooking robot"],
    "housing-real-estate": [r"\bmobile home\b", r"\bself[- ]storage\b", r"\bPatriot Holdings\b", r"\bmanufactured housing\b"],
    "machining-cnc": [
        r"\bCNC\b", r"\b5-axis\b", r"\bmachine shop\b",
        r"\bSjogren\b", r"\bDemusz\b", r"\bL&S Machine\b", r"\bAccu-Mold\b",
        r"\bAL-AN\b", r"\bturning operation",
    ],
    "consumer-electronics": [
        r"\bconsumer electronic\b", r"\bAR/VR\b",
        # Don't match "Apple"/"Snapchat" alone — they appear in many backgrounds.
    ],
    "machining-pilot-customer": [r"\bSjogren pilot\b", r"\bbearing manufactur"],  # rarely used; leave for now
}
# remove the helper non-industry "machining-pilot-customer" key
INDUSTRY_KEYWORDS.pop("machining-pilot-customer", None)

TOPIC_KEYWORDS = {
    "quality-control": [r"\bquality control\b", r"\bdefect detect", r"\bvisual inspect", r"\bCMM\b", r"\bfirst article inspect"],
    "vision-systems": [r"\bvision system\b", r"\bKeyence\b", r"\bCognex\b", r"\bcomputer vision\b"],
    "teleoperation": [r"\bteleop", r"\btele[- ]operat", r"\bVR teleop\b", r"\bALOHA\b"],
    "robotics-data": [r"\brobotics data\b", r"\brobot.+data collection\b", r"\bteleop data\b", r"\bdata for robot"],
    "imitation-learning": [r"\bimitation learning\b", r"\bIRL\b", r"\bbehavior cloning\b", r"\binverse reinforcement\b", r"\baction chunking\b"],
    "vla-vlm": [r"\bVLA\b", r"\bVLM\b", r"\bvision[- ]language[- ]action\b"],
    "tactile-sensing": [r"\btactile sens", r"\bforce/torque\b", r"\bhaptic feedback\b"],
    "manipulation": [r"\bdexterous manip", r"\bgrasping\b", r"\brobot hand\b"],
    "humanoids": [r"\bhumanoid\b", r"\bFigure AI\b", r"\bOptimus robot\b", r"\bTesla Optimus\b"],
    "simulation": [r"\bsim[- ]to[- ]real\b", r"\bdigital twin\b", r"\bIsaac Sim\b", r"\bMujoco\b"],
    "manufacturing-automation": [
        # Keep, but don't fire on every meeting that mentions "automation".
        r"\bfactory automation\b", r"\bmanufacturing automation\b",
        r"\bautomation engineer", r"\bcobot\b", r"\bAGV\b",
    ],
    "fundraising": [r"\bVC\b", r"\bSeries [ABCD]\b", r"\bpre[- ]seed\b", r"\bfundrais", r"\bSAFE note\b", r"\bterm sheet\b", r"\bangel investor\b"],
    "legal-ip": [r"\bIP rights\b", r"\bpatent\b", r"\blegal counsel\b", r"\bNDA\b", r"\battorney\b"],
    "co-founder-search": [r"\bco[- ]founder\b", r"\bCTO candidate\b"],
}


def classify(meeting_id: str, title: str, body: str) -> tuple[str, list[str]]:
    """Return (primary_category, sorted_unique_secondary_tags) for one meeting."""
    # 1. Override wins
    if meeting_id in OVERRIDES:
        primary, secondary = OVERRIDES[meeting_id]
        secondary = list(secondary)
    else:
        # 2. Title rules
        primary = None
        secondary: list[str] = []
        for pattern, p, sec in TITLE_RULES:
            if re.search(pattern, title, re.IGNORECASE):
                primary = p
                secondary = list(sec)
                break
        # 3. Default: customer-discovery (most common)
        if primary is None:
            primary = "customer-discovery"

    # 4. Secondary tag enrichment from body keywords
    haystack = (title + "\n" + body).lower()
    for tag, patterns in INDUSTRY_KEYWORDS.items():
        if any(re.search(p, haystack, re.IGNORECASE) for p in patterns):
            if tag not in secondary:
                secondary.append(tag)
    for tag, patterns in TOPIC_KEYWORDS.items():
        if any(re.search(p, haystack, re.IGNORECASE) for p in patterns):
            if tag not in secondary:
                secondary.append(tag)

    # Stable order
    secondary = sorted(set(secondary))
    return primary, secondary


def inject_tags(file_path: Path, primary: str, secondary: list[str]) -> bool:
    """Replace or add a `Tags:` line just below `Granola Meeting ID:`. Returns True if changed."""
    tag_line = f"Tags: {primary}" + (" | " + ", ".join(secondary) if secondary else "")
    text = file_path.read_text()
    lines = text.splitlines()
    out = []
    inserted = False
    skipped_existing = False
    for i, ln in enumerate(lines):
        if ln.startswith("Tags: ") and not skipped_existing:
            skipped_existing = True
            continue
        out.append(ln)
        if not inserted and ln.startswith("Granola Meeting ID:"):
            out.append(tag_line)
            inserted = True
    new_text = "\n".join(out)
    if not text.endswith("\n"):
        pass
    else:
        new_text += "\n"
    if new_text == text:
        return False
    file_path.write_text(new_text)
    return True


def main():
    manifest = json.load(open(BUILT / "manifest.json"))
    counts_primary: dict[str, int] = {}
    counts_secondary: dict[str, int] = {}
    changed = 0
    for entry in manifest:
        path = BUILT / entry["filename"]
        if not path.exists():
            print(f"WARN: missing {entry['filename']}")
            continue
        body = path.read_text()
        primary, secondary = classify(entry["id"], entry["title"] or "", body)
        counts_primary[primary] = counts_primary.get(primary, 0) + 1
        for s in secondary:
            counts_secondary[s] = counts_secondary.get(s, 0) + 1
        if inject_tags(path, primary, secondary):
            changed += 1

    print(f"Tagged {len(manifest)} meetings ({changed} files changed).")
    print("\nPrimary distribution:")
    for k in sorted(counts_primary, key=lambda x: -counts_primary[x]):
        print(f"  {counts_primary[k]:3d}  {k}")
    print("\nTop 15 secondary tags:")
    for k in sorted(counts_secondary, key=lambda x: -counts_secondary[x])[:15]:
        print(f"  {counts_secondary[k]:3d}  {k}")


if __name__ == "__main__":
    main()
