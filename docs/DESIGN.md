# Workbench visual system

The desktop workbench and mobile companion use a bold, modern product register.
The user chose stronger color and visible motion while retaining the research and
trading workflows. The existing coin mark remains the identity.

## Color and type

- Palette tokens live in `apps/shared/appearance.css`; Tailwind and the mobile
  companion consume the same OKLCH values. Layout styles stay in each app.
- Settings → Appearance offers four palettes: warm white/vermilion (`ember`),
  dark ink/glacier cyan (`glacier`), mist white/berry (`berry`), and silver/electric
  blue (`volt`). The existing cobalt/ink appearance remains the default until
  a choice is made. Restore Default returns to that appearance.
- Palette choice controls surfaces, navigation, actions, the welcome panel and
  logo tile/ink. The existing coin SVG geometry is rendered as a CSS mask.
  Glacier activates dark native controls, prose, notifications and chart chrome.
- Profit/loss, unavailable data, warnings and errors retain their business meanings.
  Brand color must never imply that an operation succeeded.
- Use the platform sans-serif stack with Chinese system fallbacks. No network fonts.
- Page headings use 28 px, standard content 13–14 px, supporting labels 11–12 px.
  Financial cells use tabular figures. Tables scroll within their own container.

## Structure and interaction

- Desktop: grouped navigation rail, context bar, independently scrolling workspace.
  Tablet: icon rail with accessible link names. Small screens: labelled navigation
  disclosure, with all module destinations preserved.
- `PageHeading` owns page title, explanatory text and actions. Shared primitives own
  button, input, table, card and tab presentation; route components own business data.
- Agent suggestions populate the composer and focus it. They never submit a turn.
- Account selection is a labelled toggle button, separate from edit/delete controls.
- Tabs support arrow keys, Home/End and labelled panels. Hidden history navigation
  is not keyboard focusable on small screens.
- Motion is short and state-related: navigation selection, panel entry, disclosure
  and press feedback. Use opacity/transforms, never animated dimensions. Respect
  `prefers-reduced-motion`; do not animate financial values or virtualized messages.

## Module patterns

- Keep the four palettes independent of module layout. `workspace.css` owns the
  route-level patterns; `WorkspaceState` supplies empty/error feedback and
  in-page section links. Loading and failures must not masquerade as empty data.
- Agent history has a clear title, search and new-conversation action. The composer
  separates writing from attachment/send controls; retain virtualized history,
  draft handling, keyboard submission and cancellation behavior.
- Account cards distinguish available cash from account capital and show the same
  financial aggregates. Selection filters holdings; a visible reset returns to all.
- Strategy research keeps its existing hypothesis editor, saved versions, backtest
  controls, results and provenance. Visual changes do not retire an entry point.
- Skills and audit use searchable libraries with persistent detail panes. Skill
  content supports formatted reading and its exact source. Audit search explicitly
  covers the loaded recent window. At narrow widths selection focuses the detail;
  Return to List restores access without losing the selection.
- Skill source uses a native disclosure with an explicit expanded/collapsed label
  and a bounded, keyboard-scrollable reader. Preserve exact source text, including
  frontmatter and whitespace. Use short opacity/transform motion for reveal.
- Rules distinguish full-document Save from entry-level save-on-blur. Show unsaved
  document status and each entry's enabled state. Never change precedence or storage.
- Memory uses readable rows, status filters and source/history disclosures. Its
  capacity and statistics retain their original meanings.
- Settings, jobs and rules separate tasks into accessible tabs with mounted panels,
  keeping unsaved fields intact. The day board shows saved scheduler times only;
  selecting a station opens and focuses its corresponding editor. It is a schedule,
  never a progress indicator or evidence that a task ran.
- Task rows use aligned time typography, compact channel stacks, quiet status dots
  and explicit run actions. Channel editing opens a non-modal anchored popover,
  never expanding table rows. Native checkboxes keep immediate-save semantics,
  pending/error feedback and the minimum-one-channel rule. Escape restores focus;
  narrow screens keep the popover inside the viewport. Authoritative save receipts
  update the channel display before revalidation.
- Run/step details have keyboard-operable buttons and read-error retry.
- `interaction.css` owns shared motion and the stronger module composition. Motion
  provides moving navigation/tab indicators and short detail transitions. The Agent
  compass responds to focused/hovered prompts, never to invented market data.
  Root MotionConfig respects reduced motion; no looping decorative animation.
- Account cards foreground balance and position count. Native disclosures retain
  the full financial breakdown without changing values or account selection.
- Route-load errors show recovery actions, never developer diagnostics. Recovery
  is user initiated; do not automatically refresh a page and interrupt work.
- Settings skills use a reading list: full identity and description, secondary
  source and a non-interactive status dot. External tools distinguish configured
  from connected and retain visible failures; disclose tool names on demand.
  Connection details may be commands or URLs. Wrap long values at narrow widths.
- Tables scroll inside their sections. Do not let intrinsic table width enlarge
  the whole page on small screens.

## Mobile companion

- Phone navigation uses a labelled bottom bar for conversations, task messages and
  connection settings. It stays outside the scrolling content and respects safe
  areas. A compact bar preserves all destinations when the keyboard is open.
- Conversation lists use a prominent new-conversation action and quiet divided
  rows. Conversation reading uses the full width; assistant prose is unboxed and
  user messages retain the selected brand color. The input stays above navigation.
- Loading and failed reads remain distinct from a confirmed empty collection.
  Previously cached records stay readable during revalidation. Empty-conversation
  suggestions fill and focus the composer, never send a request or replace a draft.
- Connection state is always written alongside its indicator. Failed actions and
  unsaved drafts appear above the scrolling content so they remain visible while
  reading. Pairing, pending requests and offline caches keep their existing meaning.
- Connection settings foreground the paired device, then show four labelled color
  previews as native radios. Restore Default stays available. Task failure and
  incomplete-result colors adapt to dark mode without changing their labels.
- Press, selection, disclosure and detail transitions use 150–250 ms transforms
  or opacity. No animation delays access to content. Respect reduced motion.

## Appearance preference

- `apps/shared/appearance.ts` owns the supported IDs and preference lifecycle.
  The selection is stored under `trade-compass.appearance.v1` in origin-local
  browser storage. It does not belong to an investment account or backend config.
- Startup reads and validates the preference before React renders. The root
  `data-theme` and `dark` class are derived state; other tabs follow storage events.
  Switching does not reload a page, clear drafts or change chart data. Mobile
  offers the same choices in its connection view; devices are not synchronized.
- Unknown or missing values fall back to the original appearance. Storage errors
  still allow the current page to change and visibly report that saving failed.
  Restoring the default is reversible; existing user records are untouched.

## Delivery boundaries

The source and bundled package share the same frontend build. Do not patch assets
inside a running installed package. Preview with isolated configuration, data,
memory and disabled scheduling. Published package acceptance and stable-service
cutover follow `development-production.md` separately.
