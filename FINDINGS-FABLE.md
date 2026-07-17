# FINDINGS-FABLE.md — Review of `full-export.py`

Reviewed 2026-07-14 (all 5,367 lines). Baseline: `python test_export.py` passes.
Findings marked **[confirmed]** were reproduced against the `temporary-title/` fixture data.

> **STATUS: all 26 findings fixed (2026-07-14).** Verified after the fixes:
> `test_export.py` fully passes; a field-by-field diff of every parsed PC record
> (old code vs new) against the fixture shows *only* the three intended
> thrown-weapon attack corrections; all 1,128 fixture spells now file into
> their spellcasting entries (0 misfiled); combat-record diffs are exactly the
> intended attribution corrections; all 214 preview pages render end-to-end.
> Line numbers below refer to the pre-fix file.

---

## High severity — wrong output on the live wiki

### 1. Thrown-weapon attack rolls use Str; PF2e RAW is Dex **[confirmed]**
`_calc_strikes`, full-export.py:2296-2307.

```python
elif "thrown" in traits:
    atk_mod = str_mod
```

In PF2e, *all* ranged attack rolls — including thrown weapons — use Dexterity; Strength
applies only to thrown-weapon **damage** (which the code gets right at line 2336-2342).
The comment at 2298-2302 asserts the opposite of the rule.

Confirmed wrong on three current PCs:

| PC | Weapon | Rendered | Correct (Dex) |
|---|---|---|---|
| Cap'n Robin Stryker (Str +2/Dex +3) | Bola | +6/+1/-4 | +7/+2/-3 |
| Asteras (Str +3/Dex +1) | Javelin | +7/+2/-3 | +5/+0/-5 |
| Greykor Vonana (Str +4/Dex +0) | Javelin | +8/+3/-2 | +4/-1/-6 |

Note: for a *melee* weapon with the thrown trait (dagger, hatchet), Str is correct for
the melee use — the single rendered line can't show both. But pure thrown ranged weapons
(javelin, bola: they have a `range` and the `thrown` trait) should use Dex. Fix:
`elif "thrown" in traits and weapon_range: atk_mod = dex_mod` (or render two lines).

### 2. Spell → spellcasting-entry assignment depends on LevelDB iteration order **[confirmed]**
`_parse_character` full-export.py:2767-2770, and identically `_parse_npc` full-export.py:3414-3417.

```python
if entry_id and entry_id in spell_entries:
    spell_entries[entry_id]["spells"].append(spell_obj)
else:
    spells.append(spell_obj)   # orphan
```

Spells and their `spellcastingEntry` are processed in one pass over `items`, which come
back in LevelDB key order (random item ids). A spell whose entry document sorts *after*
it is misfiled into "Other Spells" — losing its DC/attack/slot context and rank grouping.

Confirmed in the fixture: **26 spells across 6 NPC actors** are misfiled this way
(e.g. Dedicated Druid's Fireball/Lightning Bolt, White Dragon's Ray of Frost). The PCs
are currently unaffected only because their item ids happen to sort favorably — any new
spell added to a PC can flip.

Fix: two passes — collect all `spellcastingEntry` items first, then assign spells.

### 3. `enrich_item` crashes the whole export when `description.value` is null
full-export.py:638-640.

```python
actor_desc = desc_node.get("value", "") if isinstance(desc_node, dict) else ""
if not actor_desc.strip() and comp.get("desc"):
```

`.get("value", "")` returns the default only when the key is *missing*. Foundry can store
`"description": {"value": null}`, in which case `actor_desc` is `None` and `.strip()`
raises `AttributeError` — and since `_parse_character` has no try/except around
enrichment, one bad item kills the entire run. Use `desc_node.get("value") or ""`.

---

## Medium severity — subtly wrong numbers / attribution

### 4. Ability-boost application order miscomputes scores near 18
`_ability_from_boosts`, full-export.py:1583-1664.

The "+2 below 18, +1 at 18+" rule is order-sensitive, and the code applies sources in
the wrong order:

- Free-pick boosts and **apex** (build.attributes) are applied *before* ancestry/
  background/class item boosts; ancestry **flaws** are applied *after* its boosts
  (lines 1608-1620). A flaw + enough boosts to cross 18 can come out 1 low
  (e.g. flaw-then-5-boosts = 18 correct; boosts-then-flaw = 17).
- **Apex is not a boost** (line 1596-1598): RAW is "raise the score to 18, or +1 if
  already 18+". A 14 with an apex item should read 18 (+4); the code yields 16 (+3).

Fix: apply flaws first, then boosts in creation order, apex last with
`score = max(score + 1, 18) if score >= 18 else 18` semantics.

### 5. Downing attribution counts reverted damage as a hit
`_build_downing_events` Pass 1, full-export.py:866.

Source A checks `isHealing` but not `isReverted` — unlike `_build_npc_damage_kill_events`
(line 1121) and `campaign_stats` (line 1352). A GM-undone hit can still win "most recent
hit before the downing" and put the wrong attacker in `last_downed_by` / the downing log.

### 6. A downing is recorded as the attacker's "kill"
`combat_record`, full-export.py:815-817. All merged events — including PC *downing*
events — write `rec["last_kill"]` for the attacker. Friendly-fire (a PC's AoE downing an
ally) shows on the attacker's character page as `last_kill = <ally's name>`, even though
nobody died. The attacker-side write should apply only to NPC kill events (or the
event dicts need a `kind` field to distinguish).

### 7. Coin can be double-counted
`_parse_character`, full-export.py:2541-2575. The wallet sums **both** legacy
`system.currency` *and* coin treasure items. Remaster worlds store coins only as
treasure items, but a world migrated from an older schema can carry both
representations of the same money — the wallet (and wealth totals) would double.
Consider preferring treasure-item coins when any exist, else falling back to
`system.currency`.

### 8. `--push --npcs --char "Name"` pushes ALL NPCs
main, full-export.py:5317: `exporter.push_to_wiki(site, target_name=None, npcs=True)`.
Preview mode honors `--char` for NPCs (line 5359); push mode ignores it, so trying to
push one NPC pushes every NPC. Pass `target_name=args.char` (or document the asymmetry).

### 9. `--session-date` is silently ignored without `--push --session`
full-export.py:5336-5342. The flag is only parsed inside `if args.session` inside
`elif args.push`. Running `--session-date 2026-06-30` alone (or with only `--session`
in preview mode) does nothing, with no warning. Validate/warn at argparse level.

---

## Medium severity — robustness / state handling

### 10. All persistent state is CWD-relative
`_COMP_CACHE_PATH` (line 522), `_PAGE_NAMES_PATH` (726), `_NPC_APPEARANCES_PATH` (752),
`SessionExporter.SNAPSHOT_DIR` (3946) are all relative paths, while `wiki_password.txt`
correctly anchors to `Path(__file__).parent` (line 47). Running from any other directory
(a cron job without `cd`, an absolute-path invocation) silently creates a fresh empty
`session_snapshots/` — losing rename tracking, loot/XP baselines, and the session index,
and producing a garbage "everything is new loot" session page. Anchor them all to the
script directory.

### 11. NPC "Appearances" sections are always one run stale
main pushes NPC pages (line 5317) *before* `--session` updates `npc_appearances.json`
(line 5204-5207). A `--push --npcs --session` run renders NPC pages from the previous
session's appearance data. Either run the session export first or push NPCs after it.

### 12. Compendium cache not invalidated when `COMPENDIUM_PACKS` changes
full-export.py:549-557. Cache validity is mtime-only; adding a pack name to
`COMPENDIUM_PACKS` keeps serving the old cached index (missing the new pack) until a
pack file's mtime changes. Store the pack list in the cache and compare it too.

### 13. A newly-added PC's entire inventory counts as session loot
`_compute_loot`, full-export.py:4457/4471. A character (or party actor) absent from the
baseline snapshot has `prev_items = {}`, so every carried item lands in "Loot Gained"
with full gp value. Consider skipping actors with no snapshot entry (like `_compute_xp`
already does at 4534-4538).

### 14. Encounter round counts undercount
`_compute_combat_stats`, full-export.py:4256-4257. `max_round[widx]` is only advanced
inside the applied-damage branch, but `encounter:round:N` tags exist on *every*
pf2e-flagged roll message (attack rolls, saves, own-turn messages). Rounds where no
button-applied damage landed don't raise the count, deflating both the
"Encounter N (X rounds)" line and the per-round damage-taken averages' denominator.
Update `max_round` from any message carrying a round tag within the window.

### 15. Temp-dir leak on failed init
`FullExporter.__init__`, full-export.py:708-711. If `plyvel.DB()` or
`build_compendium_index` raises after `mkdtemp()`, `close()` is never reachable (the
object was never constructed) and the copied actors DB stays in /tmp. Wrap init in
try/except that cleans `self.temp_dir`.

---

## Low severity / cosmetic

16. **Docstring contradicts code** — `_get_ability_mod` (line 1548-1549) says
    "value — score ≥ 18 (treat as score)"; the code's threshold is `> 6` (line 1564),
    which the inline comment and CLAUDE.md correctly describe.
17. **Dead code** — `_read_boost_slot` can never return a list, but both call sites
    check `isinstance(chosen, list)` (lines 1613, 1619).
18. **Unused variable** — `perc_mod` in `_parse_npc` (line 3256) is computed and never
    used.
19. **`<ol>` rendered as bullets** — `html_to_wikitext` (lines 363-366) maps both
    `ul` and `ol` to `*`; ordered lists should emit `#`. Table tags (`td`/`tr`) are
    handled in `strip_html` but not in the wikitext converter, so tables in
    descriptions collapse into run-on text.
20. **Undocumented template dependency** — `_fmt_prof` (line 2824) emits
    `{{color|...}}`; the CLAUDE.md "MediaWiki requirements" section lists only
    `{{CharacterInfobox}}`. If the `color` template is missing, every proficiency
    label renders as a broken transclusion.
21. **`DEFAULT_ICONS` has no `"ammo"` entry** (line 73-86) — ammunition with a generic
    icon gets no icon at all instead of a default.
22. **Duplicate/colliding page names** — two actors with the same name (or names that
    sanitize identically, e.g. `A/B` vs `A-B`, line 3751-3761) silently share one wiki
    page, last writer wins; the rename tracker maps both ids to the same title.
23. **`wiki_img` forces square dimensions** (line 141) — `width == height` distorts
    non-square portraits; setting only `width` (or `height:auto`) would preserve
    aspect ratio.
24. **English-locale dependence** — `_APPLIED_HP_RE` (line 1067) and the condition-card
    `<span class="name">` matching only work on an English-language Foundry install.
    Fine for this campaign; worth a comment.
25. **Stale usage strings** — the module docstring (lines 9-14) and argparse epilog
    (lines 5252-5264) call the script `full-exporter.py`; the file is `full-export.py`.
26. **Old preview files linger** — `preview()` (line 3888-3894) never clears
    `wiki_preview/`, so renamed/deleted actors leave stale `.wiki` files behind (the
    deleted files in the current git status are an example of this confusion).

---

## Suggested fix order

1. (#3) `enrich_item` None-crash — one-line fix, prevents a whole-run failure.
2. (#2) Two-pass spell/entry collection — 26 confirmed misfilings in fixture NPCs.
3. (#1) Thrown-weapon Dex attack — wrong numbers on three live PC pages.
4. (#10) Anchor state paths to the script directory — prevents silent data-loss from cron/CWD.
5. (#5, #6) Combat-record attribution fixes.
6. The rest as convenient.
