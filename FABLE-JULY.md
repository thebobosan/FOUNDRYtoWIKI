# FABLE-JULY.md — Code Review Findings

Review of `full-export.py`, 2026-07-11 (post the fixes recorded in CODEREVIEWJULY11.md).
New findings only — items already documented in FABLE.md / CODEREVIEWJULY11.md are not repeated
except in the "Still open" section. Findings marked **[verified]** were confirmed empirically
against the `temporary-title` fixture (1,561 chat messages, 20 downing events).

## New bugs

### High

1. **[verified] `_build_downing_events` Source B keys hit history by full UUID — it never matches a downing victim** (~line 741).
   `victim_id = target.get("actor", "")` stores the raw `context.target.actor` value, which is a
   full document UUID (`Scene.x.Token.y.Actor.z`) in **447 of 447** such messages in the fixture —
   never a bare actor id. Downing signals look victims up by bare actor id, so every Source B
   entry is unreachable: Source B currently contributes **zero** attributions. Its documented
   purpose — catching downings where damage was applied by manual HP edit/drag with no
   `appliedDamage` flag — is silently defeated. The fixture masks this because Source A
   (`appliedDamage`) happened to cover all 20 downings, but any downing whose only preceding hits
   were manual-HP-applied will get an empty attacker.
   **Fix:** `victim_id = self._uuid_to_actor_id(target.get("actor", "")) or target.get("actor", "")`.
   (Side effect: the `atk_id != victim_id` self-hit guard is also currently comparing a bare id to
   a UUID, so it never fires either.)

2. **[verified] Dead attacker fallback in the same block** (~line 745).
   `self._uuid_to_actor_id((m.get("speaker") or {}).get("actor", ""))` — `speaker.actor` is a bare
   16-char actor id, never contains `"Actor."`, so `_uuid_to_actor_id` always returns `""` and the
   fallback is dead code. Verified: 34 of 447 `context.target` messages lack `pf2e.origin.actor`,
   and in all 34 the bare `speaker.actor` was present and discarded. Attacks whose messages carry a
   target but no origin flag lose attribution entirely.
   **Fix:** use `(m.get("speaker") or {}).get("actor", "")` directly (as the victim fallback in
   Source A already does).

3. **[verified] ChoiceSet-granted skill training is never applied — fixture PC affected** (`_rules_ranks`, ~line 1339).
   PF2e backgrounds/feats that let the player *choose* a skill store the grant as an
   ActiveEffectLike rule with a templated path:
   `system.skills.{item|flags.pf2e.rulesSelections.skill}.rank`. `PATH_MAP` only matches literal
   paths, so the rule is silently dropped. Concrete impact in the fixture: **Aargic**'s
   "Martial Disciple" background grants Trained in Athletics (evidenced by its granted Quick Jump
   feat, which requires trained Athletics), but his page renders Athletics as
   **Untrained, +2** (rank 0). Ezren's "Skilled Human (Society)" has the same shape, including a
   `ternary(gte(@actor.level,5),2,1)` value the `_int()` coercion also can't handle.
   **Fix:** resolve `{item|flags.pf2e.rulesSelections.<key>}` templates against the item's own
   flags before the PATH_MAP lookup; where the selection flag is absent (as on Aargic's copy),
   consider inferring from the granted feat's prerequisite or at least logging a warning.
   Also unhandled: `system.proficiencies.defenses.light-barding/heavy-barding.rank`
   (animal-companion barding — harmless for PC pages, listed for completeness).

4. **[verified] Foundry enricher markup leaks raw into rendered pages and tooltips** (`clean_foundry_text`, ~line 231).
   Only `@UUID[...]` and `[[/roll]]` are cleaned. `@Damage[...]`, `@Check[...]`, and
   `@Template[...]` pass through untouched — **29 PC item descriptions** in the fixture contain
   them, e.g. Elixir of Life (Minor) renders literally as
   *"you regain @Damage[1d6[healing]] Hit Points"*. These appear in inventory tooltips, feat
   descriptions, and spell description blocks.
   **Fix:** extend the regexes: `@Damage[2d6[fire]]{label}` → label (or "2d6 fire"),
   `@Check[fortitude|dc:20]` → "DC 20 Fortitude", `@Template[...]` → drop (or keep its `{label}`).

### Medium

5. **`--session-date` rebuilds destroy the historical archive snapshot** (`SessionExporter.run`, ~line 3871).
   `_save_snapshot(inventory_entities, date_str)` labels *today's* inventory with the *past*
   window's date, overwriting `snapshot_<pastdate>.json` — the one artifact that could ever make
   historical rebuilds correct — and re-baselines `snapshot_latest.json` too. Combined with the
   still-open wrong-baseline issue (FABLE.md #2), a rebuild is doubly destructive. Relatedly,
   `_read_ingame_date()` on a rebuild reports the *current* in-game date, not the session's.
   **Fix:** when `start_date` is given, skip `_save_snapshot` entirely (and skip/label the in-game
   date line).

6. **Encounter windows have no end bound** (`_compute_combat_stats._window_for_ts`, ~line 3349).
   Any message after the first initiative roll of the day is bucketed into the most recently
   started encounter, no matter how much later — post-combat healing-drag damage, a hazard hit
   hours later, or GM HP fiddling all inflate the last encounter's `max_round` and per-PC
   damage-taken stats (and thereby the per-round averages).
   **Fix:** cap each window at (last roll in cluster + `ENCOUNTER_GAP_MS`), or at the next
   window's start, and drop events falling outside every window.

### Low

7. **Skill rules-rank comparison bypasses `_rank_from_node`** (`_parse_character`, ~line 2044).
   `if rules_rank > _int(stored_data.get("rank", 0))` — a dict-shaped stored rank (`{"value": 2}`,
   which `_rank_from_node` explicitly supports) coerces to 0 here, so a lower rules rank (1) can
   win, discard the stored precomputed total, and recompute the skill at the *lower* rank.
   Use `self._rank_from_node(stored_data)` for the comparison.

8. **`combat_record()` is polluted with token-id keys.** NPC kill events carry
   `victim_id = <token id>`; the merge loop does `record.setdefault(vic_id, ...)` and writes
   `last_downed_by` entries for NPC tokens. Harmless today (lookups are by PC actor id) but any
   future consumer iterating `combat_record()` keys will see phantom "actors".
   Skip the victim-side write when the victim id isn't a known actor id.

9. **Loot tables still emit `[[Item]]` red links** (`render_session_page`, ~line 3788) even though
   inventory item links were deliberately replaced with hover tooltips (commit 9002651) precisely
   because those per-item pages don't exist. Every session page seeds red links. The snapshot
   would need to carry the item description/stat-line for a tooltip; failing that, render plain
   text like the "Items Spent / Removed" name column effectively is.

10. **Iconic-art portraits are discarded** (`icon_url`). `"iconics"` in `GENERIC_ICON_FRAGMENTS`
    means a player who deliberately picks an iconic portrait (e.g. the bundled Ezren art) gets the
    mystery-man default on the wiki. If intentional, worth a comment; if not, drop `"iconics"` for
    the actor-portrait call site.

## Still open from earlier reviews (spot-checked 2026-07-11)

- FABLE.md #1–3 — snapshot lifecycle: same-day `--session` rerun wipes loot/XP tables; snapshot
  saved before the wiki edit succeeds; `--session-date` diffs against `snapshot_latest`.
- FABLE.md #5 — hardcoded `WIKI_PASSWORD` fallback still in source.
- FABLE.md #7 — `_is_coin_item` still classifies any 1-coin-priced treasure (e.g. a 1 gp gem) as currency.
- FABLE.md #13 — the `ctx.type in ("attack-roll","damage-roll")` filter was added to
  `_build_npc_kill_events` only; `_build_downing_events` Source B still counts saving throws and
  skill checks against a target as "hits" for PC-downing attribution.
- FABLE.md #14 — session-window boundaries still double-inclusive (`start_ms <= ts <= end_ms`) in
  `_read_initiative_rolls`, `_compute_combat_stats`, and `_extract_session_data`.
- FABLE.md #17 (page-title sanitization on push), #20 (`{total_gp:g}` formatting), #21 (hero-points
  0 shown in infobox but hidden in Details), #24 (`sys` module shadowed in `_save_snapshot` /
  `_compute_loot`), #25 (bare `except:` in `_item_bulk`) — all unchanged.

## Feature suggestions

(Non-duplicates of the FABLE.md list — protected manual sections, index pages, `--dry-run`,
config file, logging, retry/exit codes, wealth history, and per-spell descriptions remain good
ideas from there.)

1. **PC Strikes section.** The math engine already has ability mods, proficiency cascade, and
   potency runes; weapons already carry damage dice via `item_stat_line`. Compute and render an
   "Attacks" table (weapon, attack bonus, damage) — the single biggest crunch gap on PC pages.

2. **Computed spell DC / attack.** `spelldc.dc` is often absent on PC spellcasting entries, so the
   Spellcasting section shows no DC. Reconstruct from key ability + level + a spellcasting
   proficiency rank cascade, same pattern as saves.

3. ✅ IMPLEMENTED (2026-07-11, `session_index.json` / `_session_nav_line` / `render_sessions_index_page`)
   **Session navigation + auto index.** Add `← previous | next →` links between `Sessions/YYYYMMDD`
   pages (the dated snapshots already enumerate session dates) and regenerate a `Sessions` index
   page with date, in-game date, encounter count, and characters present.

4. ✅ IMPLEMENTED (2026-07-11, `campaign_stats()` / `--stats`) **Campaign stats page.** `combat_record()` and the message log already yield kills, downings,
   and damage with timestamps — render a cumulative leaderboard (total kills per PC, times downed,
   damage dealt/taken across the campaign) instead of only surfacing the single most recent event
   in the infobox.

5. **In-game timeline.** `_read_ingame_date` already decodes the Seasons & Stars calendar; record
   the in-game date per session into the snapshot and render a campaign timeline page mapping
   real-world sessions to Vux calendar dates.

6. **Journal export.** Foundry journals (`data/journal` LevelDB) hold the GM's lore/handout
   content; the HTML→wikitext converter already exists. Exporting journals (with a folder-based
   include/exclude convention for GM-only entries) would populate the wiki's actual lore pages.

7. **NPC "Appearances" section.** Session data already knows which NPC tokens appeared in which
   window — list "Seen in: [[Sessions/20260630]], …" on each NPC page. Player-safe (no stats) and
   makes NPC pages useful as campaign memory.

8. **Session preview mode.** `--session` without `--push` currently just prints a note. Rendering
   `wiki_preview/Session_YYYYMMDD.wiki` (skipping the snapshot save so it stays side-effect-free)
   would let the GM proofread the recap before it goes live.

9. ✅ IMPLEMENTED (2026-07-11, `render_party_overview_page()` / `--overview`) **Party overview table.** A `Party` page row per PC — portrait, class/level, AC, saves, passive
   Perception, languages — as a GM/player quick-reference; all fields already computed.

10. ✅ IMPLEMENTED (2026-07-11, `item_unit_gp()` / `_gp_summary_block()`) **Loot valuation.** The
    wealth section already converts prices to gp; apply the same to the session loot diff and show
    "≈ N gp gained this session" plus a per-character split.
