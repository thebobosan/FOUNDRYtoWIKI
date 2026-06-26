#!/usr/bin/env python3
"""
Test script for full-export.py against the monday-alkenstar world data.

Runs the full parse + render pipeline (no wiki push, no compendium enrichment)
and reports pass/fail for each character and NPC. Use --verbose to see the
rendered wiki markup for each actor.

Usage:
    python test_export.py
    python test_export.py --verbose
    python test_export.py --char "Name"
"""

import sys
import os
import argparse
import traceback

# No WIKI_PASSWORD guard needed — the script falls back to its default

# mwclient may not be installed in dev environments — stub it out since we
# never call push functions in this test script.
import unittest.mock, sys as _sys
if "mwclient" not in _sys.modules:
    _sys.modules["mwclient"] = unittest.mock.MagicMock()

HERE     = os.path.dirname(os.path.abspath(__file__))
WORLD    = os.path.join(HERE, "monday-alkenstar")
SNAP_DIR = os.path.join(HERE, "test_snapshots")

# ── Import the module, overriding path constants ──────────────────────────────
# The filename uses a hyphen so it can't be imported normally.
import importlib.util, types

spec = importlib.util.spec_from_file_location(
    "full_export", os.path.join(HERE, "full-export.py")
)
fe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fe)

fe.WORLD_PATH   = WORLD
fe.FOUNDRY_DATA = ""   # skip compendium enrichment (no system data locally)
fe.FOUNDRY_URL  = "https://foundry.example.com"

# ── Helpers ───────────────────────────────────────────────────────────────────

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
WARN = "\033[33mWARN\033[0m"

def check(label: str, condition: bool, detail: str = "") -> bool:
    status = PASS if condition else FAIL
    suffix = f"  ({detail})" if detail else ""
    print(f"    [{status}] {label}{suffix}")
    return condition

def section(title: str):
    print(f"\n{'─'*60}")
    print(f"  {title}")
    print(f"{'─'*60}")


# ── Character checks ──────────────────────────────────────────────────────────

def validate_character(char: dict, verbose: bool) -> int:
    """Return number of failures for this character."""
    name   = char.get("name", "??")
    fails  = 0

    section(f"PC: {name}")

    # Required fields present
    for field in ("name", "level", "hp", "hp_max", "ac", "perception",
                  "speed", "abilities", "fortitude", "reflex", "will",
                  "skills", "currency", "items", "bulk_current"):
        ok = field in char and char[field] is not None
        if not check(f"field '{field}' present", ok):
            fails += 1

    # Sanity ranges
    lvl = char.get("level", 0)
    if not check("level 1–20", 1 <= lvl <= 20, str(lvl)):
        fails += 1

    hp  = char.get("hp_max", 0)
    if hp == 0:
        print(f"    [{WARN}] hp_max = 0 (character may be deceased)")
    else:
        check("hp_max > 0", True, str(hp))

    ac  = char.get("ac", 0)
    if not check("ac > 0", ac > 0, str(ac)):
        fails += 1

    perc = char.get("perception", -99)
    if not check("perception in [-5, 30]", -5 <= perc <= 30, str(perc)):
        fails += 1

    # Abilities
    ab = char.get("abilities", {})
    if check("6 ability scores", len(ab) == 6, str(list(ab.keys()))):
        for slug in ("str","dex","con","int","wis","cha"):
            mod = ab.get(slug, -99)
            if not check(f"  {slug} mod in [-5, 10]", -5 <= mod <= 10, str(mod)):
                fails += 1
    else:
        fails += 1

    # Saves (stored flat as fortitude/reflex/will totals on PC)
    for save_key in ("fortitude", "reflex", "will"):
        val = char.get(save_key, -99)
        if not check(f"  {save_key} in [-5, 30]", -5 <= val <= 30, str(val)):
            fails += 1

    # Skills (16 core)
    skills = char.get("skills", {})
    if not check("skills not empty", len(skills) > 0, str(len(skills))):
        fails += 1

    # Currency
    cur = char.get("currency", {})
    for denom in ("pp","gp","sp","cp"):
        ok = isinstance(cur.get(denom), int) and cur[denom] >= 0
        if not check(f"  currency {denom} >= 0", ok, str(cur.get(denom))):
            fails += 1

    # Render
    try:
        markup = _dummy_exporter().render_character_page(char)
        if not check("render_character_page produces output", len(markup) > 100,
                     f"{len(markup)} chars"):
            fails += 1

        # Key structural elements
        for fragment in (f"= {name} =", "CharacterInfobox", "wikitable"):
            if not check(f"  markup contains '{fragment}'", fragment in markup):
                fails += 1

        if verbose:
            print("\n── Rendered markup ──────────────────────")
            print(markup[:3000])
            if len(markup) > 3000:
                print(f"  … ({len(markup)-3000} more chars)")
            print("─────────────────────────────────────────\n")

    except Exception as e:
        print(f"    [{FAIL}] render_character_page raised: {e}")
        traceback.print_exc()
        fails += 1

    status = PASS if fails == 0 else f"{FAIL} ({fails} failures)"
    print(f"\n  Result: {status}")
    return fails


# ── NPC checks ────────────────────────────────────────────────────────────────

def validate_npc(npc: dict, exporter, verbose: bool) -> int:
    name  = npc.get("name", "??")
    fails = 0

    section(f"NPC: {name}")

    for field in ("name", "level", "hp_max", "ac", "perception",
                  "abilities", "saves", "actions"):
        ok = field in npc and npc[field] is not None
        if not check(f"field '{field}' present", ok):
            fails += 1

    lvl = npc.get("level", 0)
    if not check("level in [-1, 25]", -1 <= lvl <= 25, str(lvl)):
        fails += 1

    hp = npc.get("hp_max", 0)
    if not check("hp_max > 0", hp > 0, str(hp)):
        fails += 1

    try:
        markup = exporter.render_npc_page(npc)
        if not check("render_npc_page produces output", len(markup) > 50,
                     f"{len(markup)} chars"):
            fails += 1
        if not check("  markup contains name header", f"= {name} =" in markup):
            fails += 1
        if not check("  markup contains CharacterInfobox", "CharacterInfobox" in markup):
            fails += 1
        if not check("  markup contains [[Category:NPCs]]", "[[Category:NPCs]]" in markup):
            fails += 1

        if verbose:
            print("\n── Rendered NPC markup ──────────────────")
            print(markup[:2000])
            if len(markup) > 2000:
                print(f"  … ({len(markup)-2000} more chars)")
            print("─────────────────────────────────────────\n")

    except Exception as e:
        print(f"    [{FAIL}] render_npc_page raised: {e}")
        traceback.print_exc()
        fails += 1

    status = PASS if fails == 0 else f"{FAIL} ({fails} failures)"
    print(f"\n  Result: {status}")
    return fails


# ── Wealth section check ──────────────────────────────────────────────────────

def validate_wealth(char: dict, exporter, verbose: bool) -> int:
    name  = char.get("name", "??")
    fails = 0
    section(f"Wealth: {name}")

    try:
        markup = exporter._section_wealth(char)
        if not check("wealth section non-empty", len(markup) > 0):
            fails += 1
        if not check("  contains 'Coin'", "Coin" in markup):
            fails += 1
        if not check("  contains 'Total Value'", "Total Value" in markup):
            fails += 1
        if verbose:
            print(f"    Markup:\n{markup}\n")
    except Exception as e:
        print(f"    [{FAIL}] _section_wealth raised: {e}")
        traceback.print_exc()
        fails += 1

    return fails


# ── Dummy exporter (for calling instance methods without wiki connection) ─────

def _dummy_exporter():
    """Build a FullExporter pointed at the local test world."""
    return _cached_exporter()

_exporter_instance = None
def _cached_exporter():
    global _exporter_instance
    if _exporter_instance is None:
        _exporter_instance = fe.FullExporter(WORLD, data_root="")
    return _exporter_instance


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Test full-export.py against monday-alkenstar")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print rendered wiki markup for each actor")
    parser.add_argument("--char", metavar="NAME",
                        help="Test only this character (substring match, case-insensitive)")
    parser.add_argument("--npcs", action="store_true",
                        help="Also test NPC parsing and rendering")
    args = parser.parse_args()

    print(f"\nFoundry to Wiki — Test Suite")
    print(f"World: {WORLD}")

    try:
        exporter = _cached_exporter()
    except Exception as e:
        print(f"\n[{FAIL}] Could not open world database: {e}")
        traceback.print_exc()
        sys.exit(1)

    total_fails = 0

    # ── Player characters ─────────────────────────────────────────────────────
    print("\n\nLoading player characters…")
    try:
        all_chars = exporter.get_all_characters()
    except Exception as e:
        print(f"[{FAIL}] get_all_characters() raised: {e}")
        traceback.print_exc()
        sys.exit(1)

    if not all_chars:
        print(f"[{WARN}] No player characters found in world database.")
    else:
        print(f"Found {len(all_chars)} PC(s).")

    for char in all_chars:
        if args.char and args.char.lower() not in char.get("name", "").lower():
            continue
        total_fails += validate_character(char, args.verbose)
        total_fails += validate_wealth(char, exporter, args.verbose)

    # ── NPCs ──────────────────────────────────────────────────────────────────
    if args.npcs:
        print("\n\nLoading NPCs…")
        try:
            all_npcs = exporter.get_all_npcs()
        except Exception as e:
            print(f"[{FAIL}] get_all_npcs() raised: {e}")
            traceback.print_exc()
            all_npcs = []

        if not all_npcs:
            print(f"[{WARN}] No NPCs found.")
        else:
            print(f"Found {len(all_npcs)} NPC(s).")
            for npc in all_npcs:
                if args.char and args.char.lower() not in npc.get("name", "").lower():
                    continue
                total_fails += validate_npc(npc, exporter, args.verbose)

    # ── Summary ───────────────────────────────────────────────────────────────
    print(f"\n{'═'*60}")
    if total_fails == 0:
        print(f"  [{PASS}] All checks passed.")
    else:
        print(f"  [{FAIL}] {total_fails} check(s) failed.")
    print(f"{'═'*60}\n")

    sys.exit(0 if total_fails == 0 else 1)


if __name__ == "__main__":
    main()
