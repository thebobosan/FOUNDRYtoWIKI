# CODEREVIEWJULY11.md

Independent audit of `full-export.py` by three separate agents (re-run from scratch on 2026-07-11, post the HP-max/AC-armor-alteration/boost-slot fixes already merged that day). Findings deduplicated and ranked by confidence (number of agents that identified the issue) and severity. Each agent had access to `test_export.py` and the `temporary-title` fixture and was instructed to verify hypotheses empirically where possible rather than guess from code alone.

---

## Critical — Confirmed by All 3 Agents

### 1. ✅ FIXED `SessionExporter._extract_session_data` — NPC kills never detected; "Enemies Killed" always reads 0
**Confirmed by: Agents 1, 2 & 3 | Lines: ~3006–3043**

The session exporter determines which encountered NPCs were killed using `_build_downing_events()` — the PC-only Dying/Unconscious condition-card detector. CLAUDE.md itself documents that NPCs never generate Dying/Unconscious condition cards, and the codebase already has the correct mechanism for this, `_build_npc_kill_events()` (token-based `ActorDelta` HP check), which it simply never calls here.

All three agents verified this empirically against the `temporary-title` fixture: `_build_downing_events()` returns 20 events, 0 with NPC victims; `_build_npc_kill_events()` finds 33 real NPC kills in the same world. Every `Sessions/YYYYMMDD` page's "Enemies Killed" count/column reads `0/N` regardless of what actually happened at the table.

**Fix:** Swap in `_build_npc_kill_events()` for the NPC-kill check. Note this requires reconciling actor-id vs token-id keying (see #3 below) since `_build_npc_kill_events()` returns token ids, not actor ids.

---

## High — Confirmed by Multiple Agents

### 2. ✅ FIXED `wiki_img()` — unescaped `alt` (and `url`) enables stored XSS in raw-HTML wiki output
**Confirmed by: Agents 1 & 2 | Lines: ~89–98**

`alt_attr = alt.replace('"', "&quot;")` only escapes double quotes; `<`, `>`, `&` pass through untouched into a literal `<html>...</html>` block that MediaWiki renders as raw HTML (`$wgRawHtml = true`, required for icon rendering per CLAUDE.md). `alt` is built from Foundry item/spell/feat/deity/character names, which are freely player-editable. A name containing `"><script>alert(1)</script>` breaks out of the attribute and executes for every wiki visitor — a genuine stored-XSS vector against a player-facing wiki. `url` is not escaped either.

**Fix:** `html.escape(alt, quote=True)` and `html.escape(url, quote=True)` inside `wiki_img()`.

---

### 3. ✅ FIXED `_extract_session_data` — enemy encounter/kill counts keyed by actor id, collapsing multiple simultaneous same-type NPCs
**Confirmed by: Agents 2 & 3 | Lines: ~3016–3026**

`seen_enemies` / `enemies_encountered` dedupe on `actor_id` pulled from initiative-roll messages. CLAUDE.md documents (and `_build_npc_kill_events()` specifically works around) the fact that multiple simultaneous tokens of the same monster type share one base Actor id — e.g. 5 "Cave Scorpion" tokens in one fight. A session with 5 simultaneous Cave Scorpions reports "1 enemy encountered" instead of 5, and the per-instance count can't be recovered downstream since only one entry was ever recorded. Compounds with #1: even after fixing NPC-kill detection, the kill ratio ("X of Y killed") stays wrong unless encounter counting is also fixed.

**Fix:** Key `enemies_encountered` by token id (consistent with how `_build_npc_kill_events()` already handles this) rather than bare actor id.

---

### 4. ✅ FIXED `_build_npc_kill_events` — attacker attribution contaminated by non-damage rolls (possible healing/buff credit for a kill)
**Agent 2 | Lines: ~826–848**

The `target`-matching branch that attributes an NPC kill to the most recent hit against that token has no `isHealing`/roll-type filter — unlike the sibling `applied`-based branch, which does check `not applied.get("isHealing")`. Verified empirically: 116 non-attack roll messages (saving-throw and skill-check rolls) in the fixture carry `context.target` and pass this branch as if they were "hits." Any such roll with a later timestamp than the actual killing blow silently overrides attribution, crediting the kill to whoever cast a nearby buff/debuff/forced-save spell instead of whoever landed the finishing blow.

**Fix:** Restrict the `target` branch to attack/damage-type rolls (`ctx.get("type") in ("attack-roll", "damage-roll")`), or at minimum require `not ctx.get("isHealing")`.

---

## Medium — Confirmed by Multiple Agents (Currently Dormant)

### 5. ✅ FIXED `_calc_save` / `_calc_perception` — stale precomputed `value` not invalidated when rules-derived rank is higher
**Confirmed by: Agents 1 & 3 | Lines: ~1489–1520**

Both functions correctly merge `rules_ranks` (handles level-gated proficiency upgrades, e.g. "Expert Fortitude at level 3") into `rank`, but then return the actor's stored `value` unconditionally if present — without checking whether that stored total still corresponds to the *pre-upgrade* rank. This is the same bug class already fixed for skills (`_parse_character` ~1830–1838 explicitly discards a stale skill `value` when the rules-rank exceeds the stored rank); saves and perception were missed by that fix. Currently dormant: both agents confirmed `system.saves.*.value` / `system.perception.value` are absent for every PC in the fixture, so the stale-value path isn't exercised by current data — but it will silently misfire the moment Foundry does persist one of those fields alongside a rules-based upgrade.

**Fix:** Mirror the skills fix — discard the stored `value` when the merged `rank` exceeds what it implies, forcing recomputation.

---

### 6. ✅ FIXED `_calc_hp_max` — HP bonus rules applied without equip-state or predicate gating
**Confirmed by: Agents 1 & 3 | Lines: ~1556–1571**

Unlike `_armor_alteration()` (checks `_is_equipped` + `_predicate_facts_true`) and `_rules_ranks()` (checks `_rule_applies` for level gates), the `FlatModifier`/`ItemAlteration` HP-bonus scan in `_calc_hp_max` sums any matching rule from *any* item regardless of whether it's currently equipped or its predicate/level-gate holds. Currently dormant — the only real example in the fixture (the Toughness feat) uses a formula-string value (`"@actor.level"`) that's already skipped by the literal-int check (see #7) — but any future item/feat with a literal-int HP bonus gated by equip-state or level would be unconditionally applied.

**Fix:** Apply the same `_rule_applies`/equip checks used elsewhere before adding to the HP bonus total.

---

### 7. ✅ FIXED `_calc_hp_max` — Toughness-style `@actor.level` formula bonus silently dropped, previously masked by the `max(base, hp_val)` floor
**Agent 2 | Lines: ~1556–1571**

This is a known, documented limitation (formula-string rule values are explicitly out of scope), but Agent 2 flagged a real consequence: for the one Toughness-holding character in the fixture (Bernard Inksworth, level 1), the missing `+level` HP bonus is currently invisible only because `max(base, hp_val)` happens to fall back to his current HP, which equals his true max right now. Once he takes damage, or at higher levels (where the missing bonus scales with level), displayed `hp_max` will visibly undercount by exactly his level.

**Fix:** Special-case the extremely common `"@actor.level"` formula (→ `level`), or implement minimal evaluation for single-variable formulas of this shape, rather than leaving Toughness (one of the most commonly taken PF2e feats) silently non-functional.

---

### 8. ✅ FIXED Item/character names interpolated into wikitext unescaped — table corruption / limited injection
**Confirmed by: Agents 1 & 3 | Lines: throughout rendering, e.g. `_section_inventory` ~2233–2245, `render_character_page` ~2610–2613**

Names flow straight into wikitable rows and `[[links]]` (e.g. `f"| {wiki_img(...)} || [[{name}]] || ..."`) with no escaping of MediaWiki metacharacters. A name containing a literal `|` breaks the table row (extra/misaligned cells); a name containing `[[`/`]]` or `{{...}}` breaks the link or triggers template transclusion (e.g. `{{PAGENAME}}`). Since Foundry item/character names are freely player-editable, this is a real (lower-severity than #2, since it stays within wikitext rather than raw HTML/JS) correctness and limited-injection issue.

**Fix:** Escape or replace `|`, `[[`, `]]`, `{{`, `}}` in names before interpolating into wikitext, or wrap names in `<nowiki>`.

---

## Low — Single Agent / Minor / Edge Cases

### 9. ✅ FIXED `speed_val` silently defaults to 25 for a genuine 0 speed
**Agent 3 | Lines: ~1621, ~2369 (`_parse_character` and `_parse_npc`)**

`speed_node.get("value") or speed_node.get("total", 25)` discards a legitimately-stored `0` (immobile creature, or a stat currently reduced to 0 by a condition) because `0` is falsy, falling through to `total`/the `25` default instead.

**Fix:** `v = speed_node.get("value"); speed_val = _int(v if v is not None else speed_node.get("total", 25))`.

---

### 10. `_extract_session_data` — `num_combats` counts distinct scenes, not distinct encounters
**Agent 3 | Lines: ~3018–3019**

Two separate fights on the same map/scene within one session window collapse into `num_combats = 1`, since `data/combats` is documented as unreliable/empty and scene identity is used as a stand-in for encounter boundaries.

**Fix:** Would need to cluster initiative rolls by time gaps rather than by scene identity.

---

### 11. `_compute_xp` — empty-string `xp` not treated as "missing"
**Agent 3 | Lines: ~3148–3196**

`_get_detail_field` returns `""` (not `None`) when `details.xp.value` is absent. The skip guard (`if level is None or xp is None: continue`) doesn't catch `xp == ""`, so a character with a blank XP field (common right after a level-up in some Foundry configurations) falls through to `_int()`, silently defaulting to `0` and potentially producing a bogus "XP gained" entry on the session page.

**Fix:** `if level is None or not str(xp).strip(): continue`, applied to both the current and previous-snapshot XP.

---

### 12. Self-referential/cyclic `containerId` silently drops items from rendered inventory
**Agent 3 | Lines: ~2193–2276 (`_render_container_tree` / `_section_inventory`)**

An item whose `containerId` points to itself, or two containers whose `containerId`s form a 2-cycle, never resolves to a valid top-level root — it's simply never placed and vanishes from the rendered page with no error. Requires corrupted/unusual data to trigger; not observed in the fixture, but there's no defense against it.

**Fix:** Detect cycles when building the container tree (walk to root with a depth cap) and fall back to top-level placement.

---

### 13. `enrich_item` — enriched `traits` list is a shared mutable reference into the compendium cache
**Agent 3 | Lines: ~405–410**

`traits_node["value"] = comp["traits"]` assigns the compendium cache's list object directly rather than copying it, so every item across every character mapping to the same compendium entry shares one mutable list object (persisted across runs via `session_snapshots/compendium_cache.json`). No current code path mutates it in place, so this is currently inert — flagged as a foot-gun for future code.

**Fix:** `traits_node["value"] = list(comp["traits"])`.

---

### 14. `_build_downing_events` hit-history dedup key can drop a simultaneous second hit from the same attacker
**Agent 2 | Lines: ~600–607**

Dedup key `(ts, atk_id)` collapses two-weapon/flurry attacks or multiple damage instances from the same attacker at an identical millisecond timestamp into one `hit_history` entry. Low practical impact — the attacker credited is the same either way, this only affects which exact timestamp is used for "most recent hit before downing" attribution.

---

## Re-verified Clean (no new issues found)

All three agents independently re-checked the areas fixed earlier on 2026-07-11 — `_calc_hp_max`'s base formula, `_read_boost_slot`/`_ability_from_boosts` boost reconstruction, and `_calc_ac`/`_armor_alteration` — against real fixture data and found no regressions. Also checked and ruled out: `_get_ability_mod`'s `>6` ability-score-vs-modifier heuristic (matches the PF2e table exactly for scores 1–19), coin-value integer division (`_is_coin_item` enforces `per==1`), AC `dexCap` default of 99 (correct "no cap" semantics), and multi-source boost-order commutativity in `_ability_from_boosts` (the +2/+1-past-18 rule only depends on running boost count, not source order).

---

## Summary

| # | Function | Confirmed By | Severity |
|---|----------|-------------|----------|
| 1 | `_extract_session_data` (NPC kills never detected) | Agents 1, 2 & 3 | Critical |
| 2 | `wiki_img` (unescaped alt/url → stored XSS) | Agents 1 & 2 | High |
| 3 | `_extract_session_data` (actor-id vs token-id encounter dedup) | Agents 2 & 3 | High |
| 4 | `_build_npc_kill_events` (attacker attribution contaminated by non-damage rolls) | Agent 2 | High |
| 5 | `_calc_save` / `_calc_perception` (stale value vs rules-rank) | Agents 1 & 3 | Medium |
| 6 | `_calc_hp_max` (HP bonus rules missing equip/predicate gating) | Agents 1 & 3 | Medium |
| 7 | `_calc_hp_max` (Toughness `@actor.level` formula dropped) | Agent 2 | Medium |
| 8 | Item/character names unescaped in wikitext | Agents 1 & 3 | Medium |
| 9 | `speed_val` defaults to 25 for genuine 0 | Agent 3 | Low |
| 10 | `num_combats` counts scenes not encounters | Agent 3 | Low |
| 11 | `_compute_xp` empty-string xp not treated as missing | Agent 3 | Low |
| 12 | Cyclic `containerId` silently drops items | Agent 3 | Low |
| 13 | `enrich_item` shared mutable traits list | Agent 3 | Low |
| 14 | `_build_downing_events` hit-history dedup key collision | Agent 2 | Low |
