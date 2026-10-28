# Recon map: Wispr Flow (Windows desktop)

Scope: the whole desktop dictation product — hotkey → record → transcribe → clean → paste, plus the personalization layer (dictionary, snippets, styles, command mode, transforms, scratchpad, history). Clean-room: functionality and UX only; our transcription backend is the user's own IVRIT service on Vercel, our LLM layer is Claude.
For: the user's own daily use (Hebrew-first), no subscription.
Date: 2026-10-28

## Sources

| # | source | URL | notes |
| --- | --- | --- | --- |
| 1 | help: app navigation | https://docs.wisprflow.ai/articles/5096240724-navigating-the-wispr-flow-app-desktop-ios-and-android | hub screens, settings tree, tray menu |
| 2 | help: hands-free | https://docs.wisprflow.ai/articles/6391241694-Use-Flow-hands-free | default hotkeys, Flow Bar behavior, 20-min cap |
| 3 | help: shortcuts | https://docs.wisprflow.ai/articles/5298382595-route-dictation-directly-to-slack-email-or-calendar-with-keyboard-shortcuts | shortcut rules (≤3 keys, 4 per action, mouse buttons) |
| 4 | help: snippets | https://docs.wisprflow.ai/articles/5784437944-create-and-use-snippets | trigger rules, longest match, import |
| 5 | help: dictionary | https://docs.wisprflow.ai/articles/4052411709-teach-flow-your-words-with-the-dictionary | words, corrections, auto-learn |
| 6 | help: styles | https://docs.wisprflow.ai/articles/2368263928-how-to-setup-flow-styles | 4 styles × app categories |
| 7 | help: command mode | https://docs.wisprflow.ai/articles/4816967992-how-to-use-command-mode | hold-to-command, selection handling |
| 8 | help: smart formatting & backtrack | https://docs.wisprflow.ai/articles/5373093536-how-do-i-use-smart-formatting-and-backtrack | spoken punctuation, "actually/scratch that" |
| 9 | help: auto cleanup | https://docs.wisprflow.ai/articles/4283510616-auto-cleanup-control-how-much-flow-edits-your-dictation-beta | None / Light / Medium |
| 10 | help: transforms | https://docs.wisprflow.ai/articles/8068950331-how-to-use-transforms-beta | Polish, Prompt Engineer, custom prompts, auto-apply |
| 11 | help: scratchpad | https://docs.wisprflow.ai/articles/9618237082-using-the-scratchpad-to-save-and-edit-notes | notes editor, versions, global shortcut |
| 12 | help: using-flow collection | https://docs.wisprflow.ai/collections/7303194563-using_wispr_flow | index of 31 how-to articles |
| 13 | the user's own install | C:\Users\…\AppData\Local\wisprflow\app-1.6.1297 | screenshots only, into replica/screens/ |

## Core loop

Hold a key, talk, release — clean text appears wherever the cursor is, in any app.

## Screens

| ID | screen | route / how to reach | purpose | key components | states seen |
| --- | --- | --- | --- | --- | --- |
| S01 | Flow Bar | always-on floating pill, draggable to screen edge; System → Flow Bar visibility | start/stop/cancel dictation with the mouse; status | pill, mic/stop ■/X buttons, wand (auto-transform), level meter | idle, listening, processing, cancelled (+Undo), snoozed, hidden |
| S02 | Hub: Home | app window → Home | transcript history + stats | search box, date-grouped list, row hover (copy / flag / undo AI edit), words-this-week card, WPM/stats, shortcut reminder | empty (new user), filled, searching |
| S03 | Hub: Dictionary | sidebar → Dictionary | manage words & corrections | list, + add, "correct a misspelling" toggle, import (≤1000, ≤3MB), search, personal/shared filter | empty, filled, import |
| S04 | Hub: Snippets | sidebar → Snippets | trigger phrase → expansion | list, + add, trigger field, rich-text expansion (bold/italic/lists/links, ≤4000 chars), import | empty, filled |
| S05 | Hub: Style | sidebar → Style | per-app-category formatting style | category tabs (Work / Personal / Email / Other), style cards (Formal / Casual / very casual / Excited!) | default, selected |
| S06 | Hub: Scratchpad | sidebar → Scratchpad or global Win+Alt+S | rich-text notes editor | tabs (5/window), note cards, pin, search, version history (50), images (≤10 × 5MB), dictate-into-note with ✓/X confirm | empty, editing, history |
| S07 | Hub: Transforms | sidebar → Transforms (beta) | rewrite selected text with prompts | Polish rules toggles, Prompt Engineer, custom prompts (≤8: name, instructions, shortcut, writing samples), View Diff, auto-apply-after-dictation | list, editing prompt, diff view |
| S08 | Settings: General | Settings | shortcuts, microphone, languages | shortcut editor (Change → press keys → Done, ≤3 keys incl. modifier, ≤4 per action, Reset to default), mic picker, "My languages" multi-select + auto-detect | default, editing shortcut, conflict warning |
| S09 | Settings: System | Settings → System | OS integration | launch at login, Flow Bar visibility, notifications by category, sounds, mute music while dictating, auto-learn words, reset & restart | toggles |
| S10 | Settings: Personalization | Settings → Personalization | cleanup depth | Smart Formatting toggle, Auto Cleanup (None / Light / Medium) | selected level |
| S11 | Settings: Experimental | Settings → Experimental | gated features | Command Mode toggle, Press Enter command, bulk import | off/on |
| S12 | Settings: Data & Privacy | Settings → Data & Privacy | storage | privacy mode (zero retention), cloud sync, auto-delete transcripts, export data | on/off |
| S13 | Tray icon + menu | right-click tray | open app / help / quit | icon (state-colored), menu | idle, recording |
| S14 | Toast / notification | after each dictation or error | feedback | short text, Undo, "Done. See changes" (transforms) | success, error, no-audio, cancelled |
| S15 | Onboarding | first run | permissions, pick push-to-talk key (Ctrl+Win or Caps Lock), languages, test dictation | wizard steps | step 1..n |

## Flows

F01 Push-to-talk dictation
    S01/any app → hold PTT (Ctrl+Win or Caps Lock) → speak → release → S14 toast → text pasted at cursor
    happy path clicks: 1 (one key hold)
    edge: no audio detected (nothing pasted), mic failure, release before listening started = cancel, 19-min warning / 20-min hard stop (still transcribes), app still processing previous transcript (press ignored), paste blocked in app (shows in notification instead)

F02 Hands-free dictation
    press Ctrl+Win+Space (or double-tap PTT within 0.5s, or click Flow Bar) → speak → press again / click ■ → pasted
    happy path clicks: 2
    edge: second press within 0.5s of start = cancel; Esc = cancel (+Undo in toast); third rapid PTT tap = cancel

F03 Cancel + Undo
    while recording: Esc or X on Flow Bar → S14 "cancelled · Undo" → Undo pastes the last transcript anyway

F04 Paste last transcript
    Alt+Shift+Z → last transcript pasted at cursor

F05 Snippet expansion
    say trigger (alone or inside a sentence) → expansion replaces the trigger in the pasted text
    edge: longest matching trigger wins; personal beats team; case-insensitive match; empty expansion deletes the trigger

F06 Dictionary correction / auto-learn
    say word → wrong spelling → add correction in S03 (or edit the pasted text → auto-learn adds it, if enabled)
    edge: whole-word, case-insensitive; longer phrase wins on overlap; skips URLs/paths/code/numbers/dates/emoji/common words

F07 Backtrack (self-correction while speaking)
    "coffee at 2, actually 3" → "coffee at 3"; "scratch that", "never mind"
    edge: ordinary use of "actually" is preserved

F08 Per-app style
    dictate into Gmail → Email style (greeting, sign-off, line breaks); Slack → Work; WhatsApp → Personal; terminal/AI → Other
    edge: web apps matched by URL; style only once personalization loaded

F09 Command Mode
    focus text box (optionally select) → hold Ctrl+Win+Alt → say instruction → release → rewritten text pasted as-is (no dictation formatting)
    edge: no text and no selection = nothing; no editable field = result shown in notification; double-press locks hands-free; Esc/Backspace cancels; search commands open the browser

F10 Transform selected text
    select text → Win+Alt+1 (Polish) or custom shortcut → replaced in place → toast "Done. See changes" → View Diff / Retry / Undo
    edge: no selection = whole box, or last dictation within 60s; 1–3000 words; failed auto-apply pastes the original

F11 Scratchpad note
    Win+Alt+S → + → dictate (✓ inserts at cursor, X cancels) → auto-title → auto-save → versions

F12 Review history
    S02 → search → hover row → copy / flag / undo AI edit
    edge: privacy mode = no history; auto-delete after N days

F13 Change a shortcut
    S08 → Change → press combo → Done; Reset to default
    edge: ≤3 keys with a modifier; reserved OS combos rejected; Caps Lock alone allowed only for PTT/hands-free; overlapping PTT & hands-free combos warned

## Components

| component | variants | states | used on |
| --- | --- | --- | --- |
| Flow Bar pill | docked left/right/bottom, compact | idle, listening (level meter), processing (spinner), cancelled (Undo), hidden | S01 |
| Hotkey editor | per action, up to 4 chips | idle, capturing, conflict, reserved | S08 |
| Toggle | — | on, off, locked by org | S09–S12 |
| Style card | Formal, Casual, very casual, Excited! | selected, available, unavailable in category | S05 |
| Category tabs | Work, Personal, Email, Other | selected | S05 |
| Entry list row | dictionary word, correction (say→write), snippet (trigger→expansion) | default, hover (edit/delete), shared badge | S03, S04 |
| Add / edit sheet | word, correction, snippet, custom transform | empty, valid, duplicate error, too-long error | S03, S04, S07 |
| History row | — | hover (copy / flag / undo AI edit) | S02 |
| Stats card | words this week, WPM, days used | locked (<500 words), unlocked | S02 |
| Toast | success, error, cancelled+Undo, done+See changes | auto-dismiss | S14 |
| Tray icon | — | idle, recording, error | S13 |
| Note editor | tab, card | editing, pinned, history | S06 |
| Diff view | side-by-side | — | S07 |

## Inferred data model

Settings     hotkeys{ptt[], hands_free[], command[], cancel, paste_last, scratchpad, transforms[]},
             microphone_id, languages[], auto_detect, launch_at_login, flow_bar{visible, dock},
             sounds, mute_music, notifications{...}, auto_learn_words, smart_formatting,
             auto_cleanup (none|light|medium), command_mode_enabled, privacy_mode,
             cloud_sync, auto_delete_days
             evidence: S08–S12, sources 1–3, 9     confidence: high

DictionaryEntry  id, word (≤60), correction_from (nullable, "say X"), shared, source (manual|auto-learned|import), created_at
             evidence: S03, source 5               confidence: high

Snippet      id, trigger (≤60, trimmed, unique vs dictionary), expansion (rich text ≤4000), shared, created_at
             evidence: S04, source 4               confidence: high

StyleChoice  category (work|personal|email|other) → style (formal|casual|very_casual|excited)
AppRule      app_name_or_url_pattern → category        evidence: S05, source 6   confidence: medium (matching rules not published)

Dictation    id, started_at, duration_s, app, raw_transcript, final_text, ai_edited (bool),
             kind (dictate|command|transform|scratchpad), flagged, status (pasted|cancelled|no_audio|error)
             evidence: S02, S14, source 8          confidence: high

Transform    id, name, instructions, shortcut, writing_samples[≤5], builtin (polish|prompt_engineer|null), polish_rules{}
             auto_apply_after_dictation: transform_id|null
             evidence: S07, source 10              confidence: high

Note         id, title (auto|manual), body (rich), images[≤10], pinned (per device), versions[≤50], updated_at
             evidence: S06, source 11              confidence: high

Relationships: Settings 1-1 user; user 1-n DictionaryEntry, Snippet, Dictation, Transform, Note; StyleChoice 4 rows per user.

## Feature matrix

See `features.csv`. 62 rows — must: 23, should: 20, could: 19 (of which skip: 7). Already in flow.py: 9 done, 20 partial.

## Out of scope (cannot or should not be cloned)

- Wispr's own ASR + LLM models and their latency (we use the user's IVRIT service + Claude; quality is what those give).
- Cloud sync across devices, iOS/Android keyboards, shared team dictionary/snippets, admin portal, billing.
- Perplexity search integration, "build an app" command, LinkedIn badge, MCP server, meetings notetaker.
- Mouse-button shortcuts (Logitech aux buttons) — needs vendor drivers.
- Brand, name, icons, copy.

## Size

Screens 15, flows 13, entities 7. Hard parts: (1) reliable global hotkeys + paste into every app incl. terminals/RDP/elevated windows; (2) per-app context (active window/URL) for styles and command mode selection capture; (3) rich-text snippets/notes and paste fidelity; (4) latency — Vercel cold starts + Claude round trip vs. Wispr's streaming. Size: M (core loop S, full matrix M).
