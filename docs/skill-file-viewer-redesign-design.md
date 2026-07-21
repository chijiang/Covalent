# Skill File Viewer Redesign Design

This document specifies a rebuild of the **skill file-list + preview workbench**
in the service-console Skills page (`/service-console/skill-settings`). The
current implementation has a broken layout: file-list rows render at varying
heights and visually overlap/clip ("文件列表 item 有高有低还会遮挡"). The fix
replaces the hand-rolled, mismatched markup with standard shadcn/ui controls —
a `collapsible`-based file tree on the left and a `react-markdown`-capable
preview pane on the right, each scrolling independently.

## Table of Contents

- [Motivation](#motivation)
- [Goals and Non-Goals](#goals-and-non-goals)
- [Current State (Baseline)](#current-state-baseline)
- [Approach](#approach)
- [Component Structure](#component-structure)
- [File Explorer (Left Pane)](#file-explorer-left-pane)
- [Preview Pane (Right Pane)](#preview-pane-right-pane)
- [Layout and CSS Fix](#layout-and-css-fix)
- [Behavior Details](#behavior-details)
- [Files Touched](#files-touched)
- [Data Layer (Unchanged)](#data-layer-unchanged)
- [Testing and Verification](#testing-and-verification)
- [Risks and Open Decisions](#risks-and-open-decisions)

## Motivation

The "Bundled files" workbench shown when a skill is selected is unreadable.
File rows overlap and have inconsistent heights, so users cannot reliably click
a file or tell what is selected. The root cause is a markup/CSS mismatch (see
[Current State](#current-state-baseline)), not a data problem: the file tree
builds correctly, it just cannot lay out. We want a clean, standard,
file-explorer-and-preview experience similar to VS Code's simple file viewer,
built from the project's existing shadcn/ui primitives so it stays consistent
with the rest of the console.

## Goals and Non-Goals

**Goals**

- Eliminate the overlap / inconsistent-height bug in the file list.
- Render the file list as a proper, indented, collapsible folder/file tree with
  icons (folders expand/collapse; files are selectable).
- Make the left file list and the right preview **scroll independently**, so a
  long file list never overflows the workbench and a long file never collides
  with the tree.
- Render `SKILL.md` and other Markdown files as formatted documents via
  `react-markdown`, with a one-click toggle to raw source; render non-Markdown
  files as source. (Decision confirmed with user.)
- Add a usable preview toolbar: filename, language badge, copy button, and the
  render/source toggle.
- Extract the workbench into its own components so `skills-workspace.tsx`
  shrinks and the pieces are independently understandable.

**Non-Goals**

- No changes to the backend preview endpoint, the skill inventory list, skill
  install/upload/enable flows, or the skill manifest/registry.
- No new data fetching; reuse the existing flat `files[]` payload and the
  existing `buildPreviewTree` tree builder.
- No multi-file tabs, no diff view, no editing, no upload-from-preview. Single
  selected file, preview only.
- No theming/brand overhaul — the frontend-design direction is "clean standard
  shadcn," consistent with the existing console. (Aesthetic polish happens at
  implementation time, within the existing token system.)

## Current State (Baseline)

All skill-viewer UI lives in one file: `frontend/components/skills-workspace.tsx`
(777 lines). The relevant pieces:

- **File-list + preview JSX** — `skills-workspace.tsx:719–733`:
  ```tsx
  <div className="skill-source-workbench">
    <aside className="skill-file-list">
      {renderPreviewTree(previewTree)}   {/* raw buttons dumped in */}
    </aside>
    <section className="skill-file-preview">
      <div className="skill-file-preview-head">
        <strong>{selectedPreviewFile?.path || "Preview"}</strong>
        <span>{selectedPreviewFile?.language || "text"}</span>
      </div>
      <pre className="code-preview skill-source-preview">
        {selectedPreviewFile?.content ?? ""}
      </pre>
    </section>
  </div>
  ```
- **Tree renderer** — `skills-workspace.tsx:330–366`: recursively emits
  `.skill-tree-toggle` / `.skill-file-item` `<button>`s, indenting via inline
  `paddingInlineStart` rather than nested DOM. Chevrons are the literal text
  `>` / `v`. No icons.
- **Tree builder + expand state** — `skills-workspace.tsx:50–110`
  (`buildPreviewTree`, `PreviewTreeNode`) and `:112–127`, `:279–320`
  (`collectPreviewDirectories`, `expandedPreviewDirs` state, auto-expanded by
  default).

**The bug** (`frontend/app/globals.css`):
- `.skill-file-list` (`globals.css:4691`) is declared as
  `display: grid; grid-template-rows: auto minmax(0, 1fr); overflow: hidden;`
  — it expects exactly two rows: a header (`auto`) and a scroll body (`1fr`),
  plus child wrappers `.skill-file-list-head` / `.skill-file-list-scroll` /
  `.skill-file-tree` (defined at `:4697`, `:4714`, `:4718`).
- The JSX produces **none** of those wrappers — it drops the tree buttons
  straight into the grid container. With more than two children, grid
  auto-places the extra buttons into implicit `auto` rows. Combined with
  `overflow: hidden` and per-item `min-height: 32px` (`:4735`) inside a `1fr`
  track that cannot grow, rows collapse and the items overlap/clip at varying
  heights.
- The preview pane has the same disease: CSS at `globals.css:4800–4918`
  anticipates `.skill-file-preview-toolbar` / `-tabs` / `-pane` / `-scroll` /
  `.skill-document-preview`, but the JSX renders only a head + a bare `<pre>`.

**Stack note:** shadcn is set up (`components.json`, style `base-nova`) on
`@base-ui/react` (not Radix). Already installed and relevant: `scroll-area`,
`tabs`, `select`, `button`, `badge`, `tooltip`, `separator`. **Not** installed:
`collapsible`. `react-markdown`, `remark-gfm`, `rehype-sanitize`, and
`lucide-react` are already dependencies.

## Approach

**Chosen: shadcn `collapsible` recursive tree + `scroll-area`, with
`react-markdown` preview.** Add the `collapsible` primitive via the project's
shadcn add command, then build a recursive `<FileTreeNode>` (directories =
`Collapsible`, files = button rows). This is the canonical shadcn file-tree
pattern and reuses primitives the project already trusts.

**Alternatives considered:**

- *Hand-rolled nested tree + `scroll-area` (no new component).* Avoids adding
  `collapsible`, but is not a "standard control," which is what the user asked
  for. Rejected.
- *Flat list with path breadcrumbs (no tree).* Simplest, but loses folder
  grouping, which contradicts "文件列表管理." Rejected.

## Component Structure

Extract two new components and consume them from the workspace:

- **`frontend/components/skills/skill-file-explorer.tsx`** — the left pane.
  Props: `tree: PreviewTreeNode[]`, `selectedPath: string | null`,
  `onSelectPath: (path: string) => void`. Owns directory expand/collapse state
  internally (initialized to all-open to match current behavior). Pure
  component; no data fetching.
- **`frontend/components/skills/skill-file-preview.tsx`** — the right pane.
  Props: `file: SkillPreviewFile | null`. Owns the render/source toggle and the
  copy state. Decides render-vs-source from the file extension.
- **`frontend/components/skills/skill-file-tree-node.tsx`** — the recursive
  node used by the explorer (directory = `Collapsible`, file = row). Kept
  separate so recursion stays readable and testable.

`skills-workspace.tsx` replaces lines `719–733` with:
```tsx
<div className="skill-source-workbench">
  <SkillFileExplorer
    tree={previewTree}
    selectedPath={selectedPreviewFile?.path ?? null}
    onSelectPath={handleSelectPreviewPath}
  />
  <SkillFilePreview file={selectedPreviewFile ?? null} />
</div>
```
`PreviewTreeNode` / `SkillPreviewFile` types move (or are re-exported) from
`lib/types.ts` so the new components import them directly. The existing
`expandedPreviewDirs` state and `collectPreviewDirectories` in
`skills-workspace.tsx` are removed (expand state moves into the explorer).

## File Explorer (Left Pane)

- Container: a column with a small header row ("Files" + count badge) and a
  `ScrollArea` body. The whole pane is `min-height: 0` so the `ScrollArea`, not
  the grid, owns vertical scrolling.
- Recursive node:
  - **Directory** → `Collapsible` (default `open`). Trigger row:
    `ChevronRight` (rotates 90° when open) + `Folder` (open → `FolderOpen`) +
    name. Content = children rendered recursively as nested DOM (indentation
    via left padding on the content wrapper, **not** inline `paddingInlineStart`
    on each row).
  - **File** → a button row: `FileText` icon + name; `aria-selected`/active
    styling when `node.path === selectedPath`. Clicking calls
    `onSelectPath(node.path)`.
- Icons from `lucide-react`. Rows use the console's existing control-height and
  hover/active tokens; no new design language.
- Keyboard: rows are real buttons (focusable, `Enter`/`Space` activates). Full
  arrow-key navigation is a non-goal for this pass.

## Preview Pane (Right Pane)

- **Toolbar (top):** filename (truncated with `title` for the full path) +
  `Badge` showing the detected language + spacer + a copy `Button`
  (`navigator.clipboard.writeText(file.content)`, swaps to a check icon for
  ~1.5s, fires a `sonner` toast) + the **render/source toggle** (shown only for
  Markdown files). If `file` is null, render an empty state ("Select a file to
  preview").
- **Body** (inside a `ScrollArea`, so long files scroll without affecting the
  tree):
  - **Markdown file (`.md` / `.markdown`), render mode (default):**
    `react-markdown` with `remark-gfm` + `rehype-sanitize`, wrapped in the
    existing `.skill-document-preview` class for prose styling.
  - **Source mode (toggled), or any non-Markdown file:** a monospace code block
    using the existing `.code-preview` styling. Line numbers are a non-goal
    (nice-to-have only if trivial).
- Markdown detection: lowercased extension in `["md", "markdown", "mdx"]`.
  Non-Markdown files always show source; the toggle is hidden for them. Toggle
  state resets when the selected file changes.

## Layout and CSS Fix

This is the crux of the bug fix:

- **`.skill-source-workbench`** (`globals.css:4672`) stays a two-column grid
  (`minmax(148px,208px) minmax(0,1fr)`). Both columns become
  `min-height: 0; display: flex; flex-direction: column; overflow: hidden;` so
  each can clip and scroll independently within the workbench's `1fr` height.
- **`.skill-file-list`** (`globals.css:4691`) is rewritten to match the new
  real markup: a flex column = header (`auto`) + `ScrollArea` body (`1fr`,
  `min-height: 0`). The orphan `grid-template-rows: auto minmax(0, 1fr)`
  declaration is removed so children are never force-placed into two rows.
- **`.skill-file-preview`** (`globals.css:4800`) is rewritten to header
  (toolbar, `auto`) + `ScrollArea` body (`1fr`, `min-height: 0`), matching the
  new toolbar/body markup. The anticipated `.skill-file-preview-toolbar` /
  `-pane` / `-scroll` / `.skill-document-preview` rules are reconciled with the
  new class names (reused where they fit, removed where stale).
- Remove dead/unused rules: the old `.skill-tree-toggle`, `.skill-tree-prefix`,
  `.skill-tree-label`, `.skill-file-item` specifics that no longer match the
  new markup (replaced by Tailwind/utility classes on the new components). The
  `.skill-file-list-head/scroll/tree` and `.skill-file-preview-head/tabs`
  placeholders are either claimed by the new markup or deleted.
- **Height propagation chain** (unchanged ancestors, just confirming they
  cooperate): `.skill-source-shell` (`1fr` inside `.skill-detail-scroll`)
  → `.skill-source-workbench` → the two panes. All already use
  `minmax(0,1fr)` / `min-height:0`, so the workbench receives a bounded height
  and the per-pane `ScrollArea`s can take over scrolling.
- Responsive (`globals.css:5195–5198`): keep the narrow-width collapse to a
  single column (`grid-template-columns: 1fr`); both panes still scroll
  independently in that mode.

## Behavior Details

- **Default expand state:** all directories open on first render (matches
  today's `collectPreviewDirectories` behavior). Users can collapse.
- **Selection persistence:** selecting a file updates `selectedPreviewPath` in
  the workspace (existing handler, lightly renamed to `handleSelectPreviewPath`
  if needed). Selecting a directory does nothing (directories only toggle).
- **Markdown default view:** rendered document (not raw source) for Markdown
  files, per the user decision.
- **Copy:** whole-file content of the currently shown file.
- **Loading / empty:** reuse existing `previewLoading` and `previewFiles.length`
  guards around the workbench; the empty state lives inside the preview
  component.

## Files Touched

New:
- `frontend/components/skills/skill-file-explorer.tsx`
- `frontend/components/skills/skill-file-preview.tsx`
- `frontend/components/skills/skill-file-tree-node.tsx`
- `frontend/components/ui/collapsible.tsx` (added via shadcn)

Modified:
- `frontend/components/skills-workspace.tsx` — replace `:719–733` with the two
  components; remove `:330–366` (old renderer), `:112–127`
  (`collectPreviewDirectories`), and the `expandedPreviewDirs` state (`:279–320`
  region). Keep `buildPreviewTree` (`:50–110`) and the preview-fetch effect.
- `frontend/app/globals.css` — rewrite the `.skill-file-list`,
  `.skill-file-preview`, and related blocks (`:4662–4918` core; `:5195–5198`
  responsive) per [Layout and CSS Fix](#layout-and-css-fix).

Unchanged:
- `frontend/lib/types.ts`, `frontend/lib/client-api.ts`,
  `src/agent_framework/api/app.py` (the `GET /skills/{name}/preview` endpoint
  and its helpers).

## Data Layer (Unchanged)

The backend returns a flat `files: SkillPreviewFile[]` (`{path, language,
content}`). The client already builds the tree via `buildPreviewTree`. Neither
changes. The explorer receives the built `PreviewTreeNode[]` directly from the
workspace's existing `useMemo`.

## Testing and Verification

- **Manual (dev server, port 3100):**
  - Open `/service-console/skill-settings`, select a skill with many files and
    nested directories — confirm no overlap, consistent row height, and the
    left list scrolls independently of the right preview.
  - Select files of different types: a `SKILL.md` (renders as a document,
    toggle to source works), a script/json (source only, toggle hidden).
  - Copy button copies content and toasts.
  - Collapse/expand directories; selection highlights correctly.
  - Resize the console split; narrow the window to trigger the single-column
    responsive layout — both panes still scroll.
- **Type-check / lint:** `pnpm/npm run lint` and `tsc --noEmit` (per frontend
  tooling) must pass after the refactor.
- **No new unit tests required** for this pass (pure presentational
  components); if the project has a component-test setup, a smoke test for the
  tree rendering + markdown-vs-source decision is a nice-to-have.

## Risks and Open Decisions

- **shadcn `collapsible` on `@base-ui/react`:** must be added via the project's
  shadcn add command (not a copy-pasted Radix snippet), so it matches the
  `base-nova` style and `@base-ui/react` primitives. Mitigation: run the add
  command, read `frontend/AGENTS.md` ("This is NOT the Next.js you know") and
  the generated file before wiring it up.
- **Next.js 16 specifics:** the project pins Next 16 with breaking changes.
  Mitigation: the new components are client components (`"use client"`) doing
  only presentational work, so router-level changes are unlikely to bite; still
  follow `AGENTS.md` guidance during implementation.
- **CSS regression risk:** `globals.css` is large and shared. Mitigation:
  scope edits to the `.skill-file-list` / `.skill-file-preview` / `.skill-tree*`
  blocks and the one responsive rule; visually diff the Skills page before/after.
- **Decision locked:** Markdown default = rendered; tree default = all
  expanded; no line numbers, no multi-file tabs, no editing.
