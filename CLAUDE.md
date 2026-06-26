# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Single-file Python script (`full-export.py`) that reads a FoundryVTT PF2e world's LevelDB databases, computes character stats via a custom math engine, and pushes formatted MediaWiki pages to a private wiki.

## Running tests

```bash
# Run full test suite against monday-alkenstar world data
python test_export.py

# Run with rendered wiki markup printed per actor
python test_export.py --verbose

# Test a single character
python test_export.py --char "Name"

# Also test NPC parsing/rendering
python test_export.py --npcs
```

`test_export.py` loads `full-export.py` via `importlib.util` (the hyphen prevents normal import). It stubs out `mwclient` so no wiki connection is needed. It uses `monday-alkenstar/` as its local world fixture and sets `FOUNDRY_DATA=""` to skip compendium enrichment. Tests cover field presence, sanity ranges on stats, and that `render_character_page` / `render_npc_page` / `_section_wealth` produce valid markup.

## Running the script

```bash
# Preview all PCs (writes .wiki files to wiki_preview/)
python full-export.py

# Preview a single character
python full-export.py --char "Name"

# Push all PCs to wiki
python full-export.py --push

# Push one PC
python full-export.py --char "Name" --push

# Push PCs + NPCs + today's session log
python full-export.py --push --npcs --session

# Dump raw Foundry JSON for debugging stat issues
python full-export.py --char "Name" --debug

# Skip compendium enrichment (faster, no item descriptions/icons)
python full-export.py --no-compendium
```

Dependencies: `plyvel`, `mwclient` (install via pip). Requires access to the Foundry LevelDB files on disk.

Wiki password is read from the `WIKI_PASSWORD` environment variable (falls back to a hardcoded default in source).

## Architecture

### Data flow

1. **LevelDB read** — `FullExporter.__init__` copies the actors LevelDB to a temp dir (plyvel requires exclusive lock), then opens it. `build_compendium_index` does the same for each compendium pack.
2. **Actor parsing** — `_parse_character` / `_parse_npc` extract raw JSON from the DB. PC parsing runs the full math engine; NPCs use pre-computed values stored directly.
3. **Compendium enrichment** — `enrich_item` fills missing icons/descriptions/traits from the compendium index, keyed by normalized name (`norm_name`).
4. **Stat computation** (PCs only) — ability scores, saves, skills, AC, and perception are all reconstructed from first principles because Foundry doesn't always store pre-computed values.
5. **Wiki rendering** — `render_character_page` / `render_npc_page` produce MediaWiki markup using `{{CharacterInfobox}}`, `mw-collapsible` divs, and wikitables.
6. **Push** — `push_to_wiki` skips pages where content is unchanged (ignoring the timestamp line via `_comparable`).

### Stat math engine (PCs)

The most complex part. PF2e stores ability scores differently across Foundry versions (pre-computed mods, raw scores, or absent entirely). The resolution order in `_get_ability_mod`:
1. `system.abilities.<slug>.mod` — pre-computed modifier
2. `system.abilities.<slug>.value` > 6 — treat as ability score, convert to mod
3. Rebuild from boost/flaw slots across `build.attributes` (free-pick tiers) and ancestry/background/class/heritage items

**The `> 6` heuristic**: Foundry stores either a raw ability score (8–18+) or a pre-computed modifier (-5 to +5). Since modifiers are never > 5, a value > 6 is unambiguously a raw score. A value ≤ 6 is treated as a modifier (a raw score of 6 or below is indistinguishable from a modifier in older Foundry versions). This is the least-bad disambiguation across Foundry schema versions.

Proficiency ranks follow a similar cascade in `_save_rank` / `_perception_rank` / `_armor_prof_rank`:
1. Pre-computed rank on actor
2. `ActiveEffectLike` rules on class/class-feature items (handles level-gated upgrades like "Expert Fortitude at level 3") via `_rules_ranks`
3. Static fields on the class item
4. Hard fallbacks (all classes get Trained unarmored, all get Trained perception)

`_rank_from_node`: a `value` key on a skill/save node is a **total modifier**, not a rank — only `rank` key or plain-int nodes are ranks.

### Item prices

Item price is stored in `system.price.value` (a dict of `pp/gp/sp/cp`) and `system.price.per` (the quantity that price covers — e.g. arrows have a price per 10). Always divide by `per` and multiply by `quantity` to get total value.

### NPC rendering

`render_npc_page` is partially implemented. The infobox and description render; the full stat block (defenses, ability scores, skills, actions, spells, GM notes) is stubbed out and not yet emitted.

### Combat record

`combat_record()` / `_build_downing_events()` replays the chat message LevelDB to determine who last knocked each PC unconscious and each PC's last kill. Two attribution sources: structured `appliedDamage` flags (when "Apply Damage" button is used) and `context.target` on roll messages (catches manual HP edits).

### Session exporter

`SessionExporter` builds a `Sessions/YYYYMMDD` wiki page covering the 04:00→04:00 window. It diffs current inventory against a JSON snapshot (`session_snapshots/snapshot_latest.json`) to detect loot gained. No page is created if no combats, kills, or loot occurred that day.

## Key configuration (top of file)

- `WORLD_PATH` — path to the Foundry world directory
- `FOUNDRY_DATA` — Foundry data root (for compendium packs at `systems/pf2e/packs/`)
- `FOUNDRY_URL` — base URL for resolving icon paths
- `WIKI_URL` / `WIKI_USER` — MediaWiki connection details
- `COMPENDIUM_PACKS` — list of pf2e pack names to index

## Helper patterns used throughout

- `_int(v)` — safe int conversion, returns 0 on failure
- `_str_mod(n)` — formats int as `+N` / `-N` string
- `norm_name(s)` — lowercases and strips punctuation for compendium key lookup
- `_rank_from_node(node)` — extracts proficiency rank integer from varied Foundry node shapes

## MediaWiki requirements

- `$wgRawHtml = true` in `LocalSettings.php` — required for `<html>` tags used by `wiki_img()` to render external Foundry icon URLs
- `{{CharacterInfobox}}` template must exist on the wiki
- `mw-collapsible` is built into MediaWiki core (no extension needed)
