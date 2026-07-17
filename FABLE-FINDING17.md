# FABLE-FINDING17.md — Review of `full-export.py`

Reviewed 2026-07-16 (all 5,509 lines of the current file). Baseline: `python test_export.py`
fully passes. This is a fresh pass over the post-fix code — the 26 issues in
FINDINGS-FABLE.md (2026-07-14) are all fixed and none recur here. Structural claims
were checked against the `temporary-title/` fixture (218 actors: 198 NPC, 15 PC,
2 party, 2 familiar, 1 vehicle; 3,684 item documents).

---

## High severity — wrong output on the wiki

### 1. `--session-date` rebuilds diff *live* inventory/XP against a historical baseline — **FIXED 2026-07-16**

> **Status:** fixed. `_compute_loot`/`_compute_xp` now diff two snapshot-shaped
> views (`_snapshot_characters`); rebuilds use `snapshot_<date_str>.json` as the
> current side and warn + skip loot/XP when it's missing. Verified end-to-end
> with synthetic snapshots against the live fixture world (planted deltas came
> through exactly; live-state noise did not) plus a full `test_export.py` pass.
`SessionExporter.run`, full-export.py:5273-5278; `_compute_loot` (4555), `_compute_xp` (4629).

The baseline side is handled correctly: `_baseline_snapshot_path(date_str)` picks the
most recent dated snapshot strictly before the rebuilt window. But the "current" side
of the diff is always **today's parsed world state** (`inventory_entities = all_chars +
party_actors`, freshly read from LevelDB). Rebuilding a past window therefore reports
everything gained *since* that window as that session's loot and XP — e.g.
`--session-date 2026-06-30` run today (July 16) diffs July 16 inventory against the
June 29 snapshot, attributing two weeks of loot and XP to the June 30 page.

The `is_rebuild` logic already recognizes that today's state has "nothing to do with
that day" — but only applies that insight to *saving* the snapshot, not to the diff
itself.

**Fix direction:** when `start_date` is given, reconstruct the current side from the
dated snapshot at the window's end (`snapshot_<date_str>.json` — it stores
name/type/qty/img/unit_gp per item plus level/xp per character, which is everything
`_compute_loot`/`_compute_xp` need), and warn + skip the loot/XP sections when that
snapshot is missing rather than producing inflated figures.

### 2. 30-day snapshot pruning silently destroys rebuild baselines
`_save_snapshot`, full-export.py:4546-4553.

Dated snapshots are pruned by mtime after 30 days. They are the only artifact that
makes historical rebuilds (and the fix for #1) possible; pruning deletes them with no
trace. Worse, once *all* dated snapshots predate the cutoff (a >30-day campaign
hiatus), `_baseline_snapshot_path` falls back to `snapshot_latest.json` — exactly the
file its own docstring says a rebuild must never diff against. Dated snapshots are
small JSON files; either stop pruning, or raise the horizon dramatically and never
prune the most recent N.

---

## Medium severity — plausible wrong content / recurring push errors

### 3. Compendium index has cross-pack / cross-type name collisions
`build_compendium_index`, full-export.py:622-624; `enrich_item`, full-export.py:651.

The index is a single flat dict keyed by `norm_name(name)` (and slug) across all 11
packs. Identically-named entries in different packs overwrite each other — last pack
in `COMPENDIUM_PACKS` wins — and `enrich_item` matches an actor item of *any type*
against that flat index. An inventory item that shares a name with a spell, feat, or
class feature can silently inherit the wrong description, icon, and traits (enrichment
only fills blanks, which limits the blast radius, but a blank-description item gets
the colliding entry's text verbatim).

**Fix direction:** bucket the index by a coarse type derived from the pack
(equipment-ish / spell / feat-ish / …) and have `enrich_item` look up only the bucket
compatible with the item's own `type`.

### 4. A deleted actor's orphan page permanently jams the duplicate-title machinery
`push_to_wiki`, full-export.py:3864-3902.

Duplicate sanitized titles are disambiguated deterministically: the lowest actor id
keeps the plain title, the rest get an id suffix. If the plain-title owner is later
**deleted in Foundry**, the surviving actor becomes sole owner and its resolved title
flips from `Name (abc12)` to `Name`. The rename branch then calls `old_page.move()`
onto the deleted actor's page — which still exists on the wiki (nothing ever cleans up
pages for deleted actors) — so the move raises, the exception skips
`section[actor_id] = name`, and the same failing move is retried on **every
subsequent run** for that actor, with the page never updating.

Related missing feature: there is no `--cleanup` equivalent for `Characters/*` /
`NPCs/*` pages of actors that no longer exist in the world (only session pages have
cleanup), so orphan pages accumulate and feed exactly this failure mode.

### 5. Familiar-dealt damage and kills vanish from all combat stats
`campaign_stats` pc-filter, full-export.py:1376; `_actor_maps`, full-export.py:1052.

The fixture world has 2 `familiar` actors. Everything in the combat pipeline is
partitioned into `character` vs `npc`: a killing blow or damage whose
`origin.actor` resolves to a familiar's actor id is neither in `pc_ids` nor typed
`npc`, so the kill/damage event is silently dropped — not credited to the familiar,
not to its master, not listed anywhere. Vehicles (1 in fixture) are similarly
invisible. Minimum fix: map familiar actor ids to their master's PC id (the familiar
actor stores its master reference) when attributing dealt damage/kills.

---

## Low severity — edge cases and cosmetics

### 6. Preview filenames collide for duplicate actor names
`preview`, full-export.py:4009. Push mode disambiguates duplicate titles with id
suffixes; preview writes `Name.wiki` for both actors and the second silently
overwrites the first. The filename also passes through quotes/colons and other
filesystem-hostile characters (fine on Linux, breaks on Windows/SMB).

### 7. `--npcs` means different scopes in preview vs push
full-export.py:5455/5472 vs 5501. `--push --npcs` exports **PCs and NPCs**; plain
`--npcs` (preview) renders **NPCs only**. Both behaviors are individually documented,
but one flag meaning two scopes is a footgun — preview output is not a preview of
what the same flags would push.

### 8. `@Localize[...]` / `@Embed[...]` enrichers leak raw into pages
`clean_foundry_text`, full-export.py:329-344. Only `@UUID`, `@Damage`, `@Check`,
`@Template`, and `[[/...]]` inline rolls are stripped/resolved. Any other Foundry
enricher tag present in a description (`@Localize`, `@Embed`, `@Compendium`) renders
literally in the wiki text.

### 9. `_read_ingame_date`: dead import and UTC day-of-year
full-export.py:4133, 4143-4145. `from datetime import timezone as _tz` is never used.
More substantively, `wco.timetuple().tm_yday` computes the day-of-year of the
timezone-aware `worldCreatedOn` in UTC; a world created near local midnight can shift
the epoch offset by one day, moving every rendered in-game date by one.

### 10. `item_stat_line` renders "Reload -" for no-reload weapons
full-export.py:182-184. `reload.value` of `"-"` is the PF2e "no reload" sentinel but
is truthy, so tooltips show `Reload -`. (`"0"` correctly renders `Reload 0`, which is
a real mechanic.) Suppress the `-` sentinel.

### 11. PCs with turns but zero damage are dropped from the session combat table
`_compute_combat_stats`, full-export.py:4399. A PC who acted every round but whiffed
every attack (`dealt == taken == 0`) is omitted from the per-character table,
indistinguishable from a PC who wasn't in the fight. "0 dealt over N turns" is real
data; consider including rows for any PC with recorded own-turns.

---

## Efficiency

### 12. `_get_actor_items` full-scans the actors DB once per actor, and actor lists are re-parsed per consumer — **FIXED 2026-07-17**

> **Status:** fixed. Items are now bucketed by actor in one prefix-iterated
> pass (`_items_by_actor`, cached); `get_all_characters`/`get_all_npcs`/
> `get_party_actors`/`_actor_maps` cache their parsed results, and all actor
> scans use plyvel prefix iteration. Cold `get_all_npcs()` 1.7s → 0.30s,
> repeat calls ~2µs. Verified all 214 rendered pages (15 PC + 198 NPC +
> campaign stats) byte-identical to the old code, and `test_export.py` passes.
full-export.py:1457-1469. Every call iterates the entire actors LevelDB (~3,900 keys
in the fixture) and filters by prefix in Python. `get_all_npcs()` (198 NPCs) is
therefore ~770k key decodes — and a `--push --npcs --session --overview` run calls
`get_all_npcs()` up to 3 times (session data, `npc_by_id`, NPC push) and
`get_all_characters()` up to 3 times (PC push, session, overview), with no caching
anywhere. Two independent fixes, both easy: use plyvel's native prefix iteration
(`self.actors_db.iterator(prefix=prefix)`), or better, build one items-by-actor map
in a single pass; and cache the parsed character/NPC lists on the exporter the same
way `_raw_msgs_cache` already caches messages.

### 13. Kill/downing event builders re-run per consumer, re-copying the scenes DB each time — **FIXED 2026-07-16**

> **Status:** fixed. Both builders now cache their result on the exporter
> (`_downing_events_cache` / `_npc_kill_events_cache`, same pattern as
> `_raw_msgs_cache`); verified each builder runs exactly once across
> `combat_record()` + `campaign_stats()` (cached call ~0.5s → ~5µs) with
> identical output, and `test_export.py` fully passes.
`_build_npc_kill_events` (1205), `_build_downing_events` (863). `combat_record()`
caches only its *merged* result; `campaign_stats()` calls both builders again, and
`_extract_session_data` (4478) calls `_build_npc_kill_events` a third time. Each
kill-events run copies and scans the scenes LevelDB from scratch. Cache each builder's
output like `_raw_msgs_cache`.

---

## Missing features

### 14. No session-page preview
`--session` is push-only (preview mode prints a note and skips it). Every other page
type has a preview path; the session page — the most complex, diff-driven one — is
the only page that cannot be inspected before it goes live. A read-only preview
(render to `wiki_preview/`, skip snapshot save and index/nav writes) would fit the
existing pattern.

### 15. Session combat stats lack a "Healing Given" column — **FIXED 2026-07-17**

> **Status:** fixed. `_compute_combat_stats` now tracks button-applied,
> non-self healing per PC (same semantics as `campaign_stats`) and the
> session table renders a "Healing Given" column; pure healers with no
> damage dealt/taken now appear in the table too. Verified dealt/taken
> figures byte-identical to the old code across a whole-history window and
> per-PC healing totals bounded by the campaign-stats totals.
The campaign stats page tracks healing given per PC; the per-session table
(`_compute_combat_stats`) tracks only dealt/taken, though the same
`_applied_hp_amount` + `isHealing` data is already flowing through that loop.

### 16. FlatModifier item bonuses to saves/skills/perception are not modeled
The math engine handles resilient runes, armor potency, and armor `ItemAlteration`
rules, but a generic `FlatModifier` rule on invested gear (item bonuses to a skill,
save, or Perception — common on magic items) is ignored, understating those totals
relative to the Foundry sheet. `_calc_hp_max` already demonstrates the pattern of
reading FlatModifier rules off equipped items; the same scan could feed
saves/skills/perception selectors.

### 17. No test coverage for `SessionExporter` or the combat-record pipeline
`test_export.py` covers PC/NPC parsing/rendering and campaign-stats sanity only. Loot
diffing, XP math (level-up wraparound), encounter clustering, nav-line patching,
`--cleanup`, snapshot baselines, and kill/downing attribution — historically the
buggiest code per FINDINGS-FABLE.md and CLAUDE.md — have zero tests. Findings #1/#2
above would have been caught by a rebuild-path test with synthetic snapshots.

### 18. Familiar and vehicle actors are entirely unexported
Related to #5. Beyond stats attribution, a PC's familiar never appears anywhere on
the wiki; at minimum it could be listed on its master's character page (name,
portrait), and vehicles could get a simple flavor page like NPCs.

---

## Verified working (spot-checked, no action needed)

- All 26 FINDINGS-FABLE.md fixes remain in place (spell entry pre-pass, thrown-weapon
  Dex attack, `_applied_hp_amount` content parsing, coin-item heuristics, downing
  dedup by message id, container cycle guard, duplicate-title disambiguation, etc.).
- `test_export.py` passes end-to-end against the fixture (run 2026-07-16).
- File parses clean (`ast.parse` OK); no syntax or import-level issues.
