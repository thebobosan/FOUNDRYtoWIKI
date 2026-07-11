# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Single-file Python script (`full-export.py`) that reads a FoundryVTT PF2e world's LevelDB databases, computes character stats via a custom math engine, and pushes formatted MediaWiki pages to a private wiki.

## Running tests

```bash
# Run full test suite against temporary-title world data
python test_export.py

# Run with rendered wiki markup printed per actor
python test_export.py --verbose

# Test a single character
python test_export.py --char "Name"

# Also test NPC parsing/rendering
python test_export.py --npcs
```

`test_export.py` loads `full-export.py` via `importlib.util` (the hyphen prevents normal import). It stubs out `mwclient` so no wiki connection is needed. It uses `temporary-title/` as its local world fixture (synced from the ChaosMarine server via `sync_foundry_world.sh`) and sets `FOUNDRY_DATA=""` to skip compendium enrichment. Tests cover field presence, sanity ranges on stats, and that `render_character_page` / `render_npc_page` / `_section_wealth` produce valid markup.

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

# Push the cumulative campaign stats leaderboard page (works in preview mode too)
python full-export.py --push --stats

# Dump raw Foundry JSON for debugging stat issues
python full-export.py --char "Name" --debug

# Skip compendium enrichment (faster, no item descriptions/icons)
python full-export.py --no-compendium
```

Dependencies: `plyvel`, `mwclient` (install via pip). Requires access to the Foundry LevelDB files on disk.

Wiki password is read from the `WIKI_PASSWORD` environment variable. `make_site()` raises loudly if it's unset — there is no hardcoded fallback. Preview/non-push runs don't need it.

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

`render_npc_page` intentionally omits the NPC stat block. The wiki is player-facing, and players can read it during a session — publishing an NPC's defenses, ability scores, skills, actions, and spells would let them metagame (e.g. reading a monster's AC/resistances/attacks before or during a fight). `_parse_npc` still computes the full stat model (abilities, saves, skills, AC, HP, perception, speed, senses, IWR, actions, spellcasting, inventory) since it's needed internally (e.g. `combat_record()`, session enemy tracking), but `render_npc_page` only emits name, portrait, level, size, type, alignment, traits, languages, and flavor text (`blurb`/`pub_notes`). `priv_notes` renders as a collapsible "GM Notes (Private)" section — collapsed by default but still visible to anyone with wiki access, so treat it as GM-eyes-only by convention, not by access control. Do not "complete" this rendering to add the missing stat block unless the wiki gains a real permissions/visibility mechanism to keep it GM-only.

### Combat record

`combat_record()` returns, per actor id, `{"last_kill": {...} | None, "last_downed_by": {...} | None}`. It merges two independently-built, chronologically-sorted event lists and lets the latest timestamp win per actor:

- **`_build_downing_events()`** — PC downing events. `hit_history` (per-victim list of `(ts, attacker_id, attacker_name)`) is built from two sources: structured `appliedDamage` flags (when the "Apply Damage" button is used) and `context.target` on any non-healing roll message (catches manual HP edits/drags that never generate an appliedDamage flag). A downing is then detected from **two independent signals**, since not every downing produces a public chat message:
  - Plain "condition card" messages (no `pf2e` flags) whose `<span class="name">` tags include Dying/Unconscious. Match against the extracted condition *names* only, never the raw HTML blob — condition tooltips carry full rules text (e.g. the Wounded condition's tooltip prose mentions "unconscious"), and a raw substring search on the whole blob false-positives on that. This bug shipped for a while and corrupted `last_downed_by` on at least one character page before being caught.
  - Any `pf2e`-flagged message whose `context.options` includes a `self:condition:dying`/`self:condition:unconscious` tag (actor resolved via `context.actor`, falling back to `speaker.actor` since `context.actor` is often unset on healing-received messages). This catches downings that never got a broadcast condition card at all — e.g. a PC can go Dying from an AoE spell mid-combat, get healed a few seconds later with no card ever posted, and the only surviving evidence is the "current conditions" snapshot embedded in that healing roll's `context.options`.
  Each signal timestamp gets attributed via the most recent `hit_history` entry at or before it.

- **`_build_npc_kill_events()`** — NPC kills. Chat messages alone cannot detect most NPC deaths: NPCs never get a Dying/Unconscious condition card (that's a PC-only mechanic — confirmed by exhaustively scanning a full world's message log for any Dead/Dying/Unconscious card, HP-reaches-0 `appliedDamage` event, or "dead" token status effect tied to an NPC, and finding none), and the killing blow is frequently applied by dragging the token's HP bar to 0 directly rather than through the chat "Apply Damage" button — which leaves zero trace in the message log. The reliable, general signal turned out to be **the per-token `ActorDelta` document** in the scenes LevelDB (`!scenes.tokens.delta!<sceneId>.<tokenId>.<deltaId>`), which overrides HP for that specific unlinked-token instance independently of the shared base Actor. GMs routinely reuse/reset a base Actor as a stat-block template across unrelated later encounters (e.g. the same "Cave Scorpion" Actor document got renamed and its HP reset to full when repurposed as a "Crystal Claw" in a later session) — so the base Actor's current HP/name is not trustworthy history, but each token's own delta HP is. `_build_npc_kill_events()` treats delta `hp.value <= 0` on an actor-type `npc` token as a confirmed kill, attributed via the *last* hit recorded against that specific **token id** (not actor id — see below) anywhere in the chat log.

  A narrower, monster-specific signal also exists and was evaluated but not used as the general mechanism: some NPC stat blocks have an explicit "reduced to 0 Hit Points" reaction (e.g. Reefclaw's "Death Frenzy"), which *does* post a real, token-scoped chat card when it fires. It's a nice confirmation signal but only covers monsters with such an ability — most (e.g. Cave Scorpion) have none, so it doesn't generalize.

  **Known limitation, left as-is:** attacker/victim *names* are resolved via `name_by_actor`, keyed by actor id and reflecting the actor's *current* name — not its name at the time of the historical event. Because base Actors get reused as templates, a kill/downing attributed to a monster that has since been renamed will display under its current name (the Cave Scorpion → Crystal Claw example above). Token names (`scenes.tokens!` docs) are more accurate for NPC display names and are already preferred where available in `_build_npc_kill_events()`, but attacker names for PC-downing events still go through `name_by_actor`.

- **Token id vs actor id:** multiple simultaneous instances of the same monster type (e.g. 5 Cave Scorpion tokens in one fight) share a single base Actor id, so actor-id-keyed hit tracking merges their combat histories together and misattributes kills between them. `_build_npc_kill_events()` keys entirely on token id (via `_uuid_to_token_id`, extracting the `Token.xxx` segment from a UUID) to keep each instance's hits and death separate. `_build_downing_events()` still keys PCs by actor id, which is fine in practice since PCs are effectively one token each.

- **`data/combats` is typically empty** — don't rely on it for round/turn numbers or a combatant "defeated" flag (checked: 0 combat documents in the reference world despite dozens of real fights having happened). Per-round data does exist, just not there: individual PF2e roll messages embed `encounter:round:N` / `encounter:turn:N` tags inside `context.options`, which is the only place round/turn numbers survive.

### Campaign stats page

`campaign_stats()` aggregates the same event sources campaign-wide (no session window) into a per-PC record: kill count (from `_build_npc_kill_events`), times downed, damage dealt/taken and healing given (from `appliedDamage` flags only — manual HP-bar edits leave no message to sum, so totals undercount), plus "nemesis" (most frequent downer) and "favorite prey" (most-slain victim). Raw downing *signals* are collapsed into downing *episodes* per victim using a 10-minute gap (`DOWNING_EPISODE_GAP_MS`): one downing generates multiple signals (the condition card plus every recovery-check/heal roll made while still Dying), so counting raw signals would badly inflate "times downed". PCs with zero activity (test sheets) are excluded from the leaderboard. `render_campaign_stats_page()` emits the "Campaign Stats" page (leaderboard + collapsible kill/downing logs); pushed/previewed via `--stats`. Player-facing like the rest of the wiki — only PC stats and already-witnessed NPC token names, no NPC stat blocks.

### Session exporter

`SessionExporter` builds a `Sessions/YYYYMMDD` wiki page covering the 04:00→04:00 window. It diffs current inventory against a JSON snapshot (`session_snapshots/snapshot_latest.json`) to detect loot gained. No page is created if no combats, kills, or loot occurred that day.

## Key configuration (top of file)

- `WORLD_PATH` — path to the Foundry world directory. Points at the live ChaosMarine server path (`/var/www/java/foundry.atkennedy.com/foundrydata/Data/worlds/temporary-title/`) — correct as-is for production runs on the server.
- `FOUNDRY_DATA` — Foundry data root (for compendium packs at `systems/pf2e/packs/`). Also a ChaosMarine server path (`/var/www/java/foundry.atkennedy.com/foundrydata/Data`) — correct as-is for production runs on the server.
- `FOUNDRY_URL` — base URL for resolving icon paths
- `WIKI_URL` / `WIKI_USER` — MediaWiki connection details
- `COMPENDIUM_PACKS` — list of pf2e pack names to index

## Local development data (this machine)

This machine doesn't run against the live server paths above — it works from a local mirror synced down via `sync_foundry_world.sh` (uses the `ChaosMarine` SSH alias):

- `./temporary-title/` — mirror of the live world, used as the `test_export.py` fixture (`WORLD_PATH` override)
- `./systems/pf2e/packs/` — mirror of the compendium packs, matching the `<FOUNDRY_DATA>/systems/pf2e/packs` layout `build_compendium_index` expects

`monday-alkenstar/` (an older world fixture with a stale LevelDB schema — top-level `actors.ldb` instead of `data/actors/`) has been removed; `temporary-title` is the only local fixture now.

To exercise `full-export.py` locally against real data (not just `test_export.py`'s stubbed run), pass `--world ./temporary-title --data .` so `FOUNDRY_DATA` resolves to the local `systems/pf2e/packs/` mirror instead of the hardcoded server path.

Re-sync with `./sync_foundry_world.sh temporary-title` (world + packs) or `./sync_foundry_world.sh --packs-only` (packs only).

`wiki_preview/` (generated `.wiki` output from preview runs) is not checked in and gets recreated on demand — safe to delete anytime.

## Helper patterns used throughout

- `_int(v)` — safe int conversion, returns 0 on failure
- `_str_mod(n)` — formats int as `+N` / `-N` string
- `norm_name(s)` — lowercases and strips punctuation for compendium key lookup
- `_rank_from_node(node)` — extracts proficiency rank integer from varied Foundry node shapes

## MediaWiki requirements

- `$wgRawHtml = true` in `LocalSettings.php` — required for `<html>` tags used by `wiki_img()` to render external Foundry icon URLs
- `{{CharacterInfobox}}` template must exist on the wiki
- `mw-collapsible` is built into MediaWiki core (no extension needed)
