# CODEREVIEW.md

Independent audit of `full-export.py` by three separate agents. Findings deduplicated and ranked by confidence (number of agents that identified the issue) and severity.

---

## Critical — Confirmed by Multiple Agents

### 1. `_save_rank` / `_perception_rank` — stored rank silences level-gated proficiency upgrades
**Confirmed by: Agents 1 & 3 | Lines: ~1062–1064, ~1094–1100**

Both functions return immediately when a non-zero stored rank is found (`if rank: return rank`), before `rules_ranks` is consulted. A character whose class upgrades Fortitude to Expert at level 7 (via `ActiveEffectLike`) will show Trained if Foundry already stored rank=1 on the actor node — the upgrade is silently dropped.

`_parse_character` correctly resolves this for skills (lines ~1481–1486) using `max(stored_rank, rules_rank)`. Saves and perception lack that same merge.

**Fix:** Replace `if rank: return rank` with `rank = max(rank, rules_rank_lookup)` before falling through to class-item static fields.

---

### 2. `_ability_from_boosts` — class key ability boosts every eligible option instead of the chosen one
**Confirmed by: Agents 1 & 2 | Lines: ~884–895**

`keyAbility.value` on the class item is the list of all allowed options (e.g., `["str","dex"]` for Ranger), not the player's selection. The player's actual choice is stored on the actor at `system.details.keyability`, which this function ignores. For any multi-option class, every ability in the list receives the class boost, inflating ability scores.

**Fix:** Read `system.details.keyability` from the actor and only apply the boost to the matching slug. Fall back to `keyAbility.value` only if it is a plain string.

---

### 3. `_read_boost_slot` / `_ability_from_boosts` — unselected short-list boost slots boost all options
**Confirmed by: Agents 1 & 2 | Lines: ~442–444, ~873–876**

`_read_boost_slot` returns the raw list for any short-option slot (ancestry "boost STR or CON") when no `selected` key is present. The caller checks `isinstance(chosen, list) and slug in chosen`, which applies +2 to every ability in the list. Only the 6-element free-pick sentinel is guarded. A character mid-creation or with an unrecorded selection will have inflated scores.

**Fix:** Return `None` for any list-valued slot without a `selected` key (not just the 6-element sentinel).

---

### 4. `_parse_character` / `_section_wealth` — treasure items double-counted in total wealth
**Confirmed by: Agents 1 & 3 | Lines: ~1378–1397, ~1737–1758**

Two overlapping bugs compound each other:

- **Bug 4a** (`_parse_character`): The currency loop adds ALL `type == "treasure"` items' coin-denominated prices to `currency`, including gems, art objects, and anything with a gp price — not just actual coin stacks. A 500 gp diamond adds 500 to `currency["gp"]`.
- **Bug 4b** (`_section_wealth`): `item_gp` iterates all physical items (including those same treasure items) and adds their prices again. Combined with 4a, treasure items are counted at least twice in the total.

`_is_coin_item()` already exists and enforces the correct invariant; it is used for bulk but not here.

**Fix 4a:** Gate the `_parse_character` currency loop on `_is_coin_item(item)`.  
**Fix 4b:** Skip `type == "treasure"` items in the `item_gp` accumulation in `_section_wealth`.

---

### 5. `_section_spells` — orphan spells sort lexicographically, rank 10 sorts before rank 2
**Confirmed by: Agents 2 & 3 | Line: ~1924**

`sorted(orphans, key=lambda s: (str(s["rank"]), s["name"]))` converts rank to string, giving the order `"1", "10", "2", "3"`. The main spell-entry sort at ~1574 correctly uses `_int(s["rank"], 99)`.

**Fix:** Use `(_int(s["rank"], 99), s["name"])` as the sort key for orphans.

---

## High — Single Agent, High Credibility

### 6. `_calc_skill` — `_rank_from_node` called on pre-extracted value, not full node
**Agent 3 | Line: ~1236**

`_calc_skill` calls `self._rank_from_node(skill_data.get("rank", 0))`, pre-extracting the rank field. `_rank_from_node` expects the full node dict and looks for a `"rank"` key inside it. When Foundry stores rank as `{"value": N}`, the call becomes `_rank_from_node({"value": N})`, which finds no `"rank"` key and returns 0 (Untrained). The proficiency badge is wrong while the skill total may still be correct via `skill_data["value"]`.

**Fix:** Pass the full node: `self._rank_from_node(skill_data)`, consistent with how `_save_rank` and `_perception_rank` call it.

---

### 7. `_section_wealth` / `_section_inventory` — dict-format `quantity` returns 0
**Agent 1 | Lines: ~1748, ~1848**

When `quantity` is stored as `{"value": 3}` (valid in some Foundry schema versions), `_int({"value": 3})` raises `TypeError` internally and returns 0. Inventory rows display quantity 0 and wealth totals are multiplied by 0 for affected items. The snapshot code has the same defect.

**Fix:** Where quantity is read, unwrap dict format: `qty = _int(q["value"] if isinstance(q, dict) else q)`.

---

## Medium — Single Agent, Credible

### 8. `_armor_prof_rank` — name-based fallback grants heavy armor proficiency to light-armor classes
**Agent 1 | Lines: ~1151–1158**

The fallback block returns Trained in all armor categories (including heavy) for Ranger, Investigator, Swashbuckler, Magus, and Gunslinger — none of which have heavy armor proficiency. This only fires when all structured lookups fail, but when it does it produces wrong AC values.

**Fix:** Per-class armor category caps in the fallback block.

---

### 9. `_build_downing_events` Source B — healing rolls credited as attacks
**Agent 1 | Lines: ~606–619**

Source A filters `isHealing` explicitly; Source B does not. Any roll message with `context.target` (healing spells, Aid, Bon Mot) is appended to `hit_history`. A healer who targets a PC last can be credited as the one who downed them.

**Fix:** Check `not applied.get("isHealing")` (or equivalent roll type flag) in Source B.

---

### 10. `_build_downing_events` — `name_by_actor` populated from item sub-documents
**Agent 3 | Lines: ~544–552**

The LevelDB iterator returns both actor records (`!actors!<id>`) and item records (`!actors.items!<actor_id>.<item_id>`). Item records also have `_id` and `name` fields. These get inserted into `name_by_actor` keyed by item ID. A damage message whose actor UUID collides with an item `_id` would show an item name as the attacker.

**Fix:** Filter to records whose raw key starts with `!actors!` (not `!actors.items!`).

---

### 11. `main` — `--char` filter passed to NPC push, silently skips all non-matching NPCs
**Agent 2 | Line: ~2751**

When `--char "Name" --npcs --push` is used, `target_name=args.char` is passed to the NPC push. All NPCs that don't match "Name" are silently skipped. `--char` is documented as a PC filter only.

**Fix:** Pass `target_name=None` for the NPC push, or introduce a separate `--npc-char` flag.

---

### 12. plyvel iterators never explicitly closed — resource leak
**Agent 2 | Lines: ~694, 706, 717, 728**

`self.actors_db.iterator()` is used in bare `for` loops without `.close()` or a context manager. plyvel iterators hold a LevelDB snapshot; relying on CPython reference-counting is fragile. In `get_all_characters`, `_get_actor_items` is called inside the outer loop, creating O(characters) open iterators concurrently.

**Fix:** Use `with self.actors_db.iterator() as it:` or close in a `finally` block.

---

## Low — Minor / Edge Cases

### 13. `_build_downing_events` Source A — speaker used as victim when UUID unparseable
**Agent 1 | Lines: ~599–601**

When `uuid` fails to resolve, the fallback is `speaker.actor`. For `appliedDamage` messages the speaker is the player who clicked "Apply Damage" (typically the attacker), not the victim. The subsequent `atk_id != victim_id` guard usually prevents a bad entry but silently drops what should be a valid hit record.

---

### 14. `debug_dump` — no actor-type check; item records can match by name
**Agent 2 | Lines: ~728–735**

If the target character is absent and an item shares the name, `debug_dump` processes the item record and dumps wrong system data.

**Fix:** Add `actor.get("type") in ("character", "npc")` guard after the name check.

---

### 15. `collapsible` — title not HTML-escaped
**Agent 2 | Line: ~258**

`title` is interpolated raw into `<b>{title}</b>`. A `<`, `>`, or `&` in a section title breaks the surrounding HTML and can prevent `mw-collapsible` from toggling.

**Fix:** `html.escape(title)` before interpolation.

---

### 16. `_comparable` — MULTILINE regex can strip lines other than the timestamp
**Agent 2 | Line: ~2283**

`re.sub(r"^''Last synced:.*?''\n?", ...)` with `re.MULTILINE` strips any matching line anywhere in the page. If a character's backstory contains that literal pattern, the page is incorrectly treated as unchanged and the push is silently skipped.

**Fix:** Anchor to end-of-string (`\Z`) or strip only the final line.

---

## Summary

| # | Function | Confirmed By | Severity |
|---|----------|-------------|----------|
| 1 | `_save_rank` / `_perception_rank` | Agents 1 & 3 | Critical |
| 2 | `_ability_from_boosts` (class key ability) | Agents 1 & 2 | Critical |
| 3 | `_read_boost_slot` (unselected short-list) | Agents 1 & 2 | Critical |
| 4 | `_parse_character` / `_section_wealth` (double-count) | Agents 1 & 3 | Critical |
| 5 | `_section_spells` (orphan sort) | Agents 2 & 3 | Critical |
| 6 | `_calc_skill` (`_rank_from_node` on pre-extracted value) | Agent 3 | High |
| 7 | `_section_wealth` / `_section_inventory` (dict quantity) | Agent 1 | High |
| 8 | `_armor_prof_rank` (heavy armor fallback) | Agent 1 | Medium |
| 9 | `_build_downing_events` Source B (healing rolls) | Agent 1 | Medium |
| 10 | `_build_downing_events` (`name_by_actor` + items) | Agent 3 | Medium |
| 11 | `main` (`--char` leaks to NPC push) | Agent 2 | Medium |
| 12 | plyvel iterator leak | Agent 2 | Medium |
| 13 | `_build_downing_events` Source A (speaker fallback) | Agent 1 | Low |
| 14 | `debug_dump` (no actor-type guard) | Agent 2 | Low |
| 15 | `collapsible` (title not HTML-escaped) | Agent 2 | Low |
| 16 | `_comparable` (MULTILINE regex) | Agent 2 | Low |
