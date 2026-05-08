# Frontend recipe — streaming agent demo UI

Opinionated notes on how the `jepras-sandbox/ui/web/` frontend was built. Meant as a reference for a reusable Skill that scaffolds similar UIs (chat transcripts with streaming events, tool-call drawers, markdown rendering, a config picker, and an optional mock-business-system side panel).

---

## 1. Stack

| Concern | Pick | Why |
|---|---|---|
| Build tool | **Vite** | Fast HMR, zero-config TS, tiny dev loop |
| Language | **TypeScript (strict)** | Catches event-shape drift early |
| Styling | **Tailwind v3** + CSS variables | Utility-first; vars let shadcn theming swap clean |
| Components | **shadcn/ui (new-york, neutral)** | Copy-in primitives, not a dep; no theme lock-in |
| Routing | **react-router-dom v6** | `Outlet` nesting = layout-as-route |
| Markdown | **react-markdown + remark-gfm** | Safe HTML, tables/checklists for free |
| Icons | **lucide-react** | Tree-shakable, pairs with shadcn |
| IDs | **uuid** | Session IDs generated client-side |

Package manager: **yarn**. Node 20+. No ESLint/Prettier pinned — kept the surface minimal.

**Not used:** `EventSource` (can't POST), TanStack Query (overkill for SSE), Zustand/Redux (`useState` is enough), shadcn sidebar (too heavy for a focused chat view).

---

## 2. Project shape

```
web/
  index.html                 # root mount
  vite.config.ts             # + dev proxy /api → backend
  tailwind.config.js         # shadcn neutral palette + dark mode
  tsconfig.json              # @/* → ./src/*
  src/
    main.tsx                 # ReactDOM root
    App.tsx                  # <BrowserRouter> + <Routes>
    index.css                # tailwind directives + CSS vars + .markdown styles
    layout/
      SiteLayout.tsx         # header + <Outlet />
    routes/
      ConfigListPage.tsx     # GET /api/configs → card grid
      ChatPage.tsx           # transcript + prompt input
    components/
      ui/                    # shadcn primitives (copied in)
        button.tsx card.tsx textarea.tsx badge.tsx
        collapsible.tsx scroll-area.tsx separator.tsx
      EventRow/              # one file per event type
        UserMessage.tsx AgentText.tsx ToolCall.tsx
        ToolResponse.tsx ThinkingBlock.tsx AgentLabel.tsx
        SystemEvent.tsx
      Transcript.tsx         # reducer: events[] → rows[]
      PromptInput.tsx        # textarea + send
    lib/
      utils.ts               # cn()
      types.ts               # TS mirror of backend types
      stream.ts              # fetch+ReadableStream SSE reader
      api.ts                 # plain fetch wrappers
```

Two levels of components: `components/ui/*` are presentational primitives copied from shadcn; everything else is domain. Never import from `ui/` into another `ui/` file — keeps primitives swappable.

---

## 3. Tailwind + shadcn setup

- **CSS variables over classes** for theming — every color is `hsl(var(--background))`, etc. Dark mode is `.dark` on `<html>`. Lets you re-skin by swapping a handful of vars.
- **`baseColor: "neutral"`** in `components.json` — grayscale default plays well on demo screens.
- **`style: "new-york"`** — denser, flatter, looks better in chat UIs than "default".
- Keep primitives literal copies of shadcn source. Do **not** add behavior into `ui/button.tsx` — extend via variants or compose outside.
- Add `.markdown` styles directly in `index.css` (agent text needs paragraph spacing, code blocks, lists). Don't reach for a markdown preset plugin.

```css
.markdown p  { @apply my-2 leading-relaxed; }
.markdown ul { @apply my-2 list-disc pl-6; }
.markdown pre { @apply my-2 overflow-x-auto rounded bg-muted p-3 text-sm; }
```

---

## 4. Routing pattern

Layout-as-route with `<Outlet />`:

```tsx
<Routes>
  <Route element={<SiteLayout />}>
    <Route path="/" element={<ConfigListPage />} />
    <Route path="/session/:sessionId" element={<ChatPage />} />
    <Route path="*" element={<Navigate to="/" replace />} />
  </Route>
</Routes>
```

- `useParams` for IDs, `useSearchParams` for query strings (e.g. `?yaml=pipeline.yaml`).
- Session IDs are generated on the *link* in the config list: `<Link to={\`/session/\${uuid()}?yaml=\${encodeURIComponent(c.relpath)}\`}>`. Gives every session a stable, shareable URL without needing a round-trip.

---

## 5. Streaming from the browser

`EventSource` **won't work** — it's GET-only, and prompts need a body. Use `fetch` + `ReadableStream` and parse `data: ...\n\n` frames yourself. Pattern:

```ts
// lib/stream.ts
export async function* streamEvents(req, signal) {
  const res = await fetch("/api/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(req),
    signal,
  })
  const reader = res.body!.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  while (true) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let idx
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, idx)
      buffer = buffer.slice(idx + 2)
      if (!frame.startsWith("data: ")) continue
      const payload = JSON.parse(frame.slice(6))
      if (payload.done) return
      yield { kind: "event", event: payload }
    }
  }
}
```

Expose as an **async generator** returning a tagged union (`{kind:"event"|"error"|"done"}`) so consumers handle all three without try/catch spaghetti.

Always wire an `AbortController` from the component: `useRef<AbortController>()`, `ac.abort()` on new send or unmount.

---

## 6. Transcript reducer — the one clever bit

Events stream in as a flat array. The UI renders *rows*, which are a slightly different shape:

- Multiple partial AGENT_MESSAGE deltas collapse into one streaming bubble.
- Tool call + tool response are separate rows but share a `tool_call_id`.
- First row per author gets a `<AgentLabel>` chip.

```ts
type Row =
  | { kind: "user"; key; text }
  | { kind: "agent"; key; author; text; streaming: boolean }
  | { kind: "thinking"; key; author; text }
  | { kind: "tool_call"; key; author; part }
  | { kind: "tool_response"; key; part }
  | { kind: "system"; key; text; tone?: "info"|"error" }
```

Reducer keeps a `Map<streamKey, rowIndex>` where `streamKey = \`${invocation_id}::${author}\``. Partial deltas append to that row's text; a non-partial finalizes it. New `invocation_id` or `author` = new bubble.

```ts
const streamingByKey = new Map<string, number>()
for (const ev of events) {
  // ... for each part:
  if (p.type === "text") {
    const k = `${ev.invocation_id}::${ev.author}`
    const i = streamingByKey.get(k)
    if (ev.partial) {
      if (i !== undefined) rows[i].text += p.text
      else { rows.push({kind:"agent", ...}); streamingByKey.set(k, rows.length-1) }
    } else {
      if (i !== undefined) { rows[i].text = p.text; rows[i].streaming = false; streamingByKey.delete(k) }
      else rows.push({kind:"agent", ...})
    }
  }
}
```

This reducer is pure — run on every render with `useMemo`. No class state, no effects.

Auto-scroll: a `<div ref={endRef} />` at the bottom + `useEffect(() => endRef.current?.scrollIntoView({behavior:"smooth"}), [rows.length])`.

---

## 7. Row components

One component per row kind. Each has one responsibility and accepts exactly the slice it needs.

**Visual vocabulary:**
- **User bubble** — right-aligned, `bg-primary text-primary-foreground`, rounded-2xl with `rounded-br-sm` for the tail.
- **Agent bubble** — left-aligned, `bg-muted`, rounded-2xl with `rounded-bl-sm`. Renders markdown.
- **Streaming cursor** — `<span className="animate-pulse h-3 w-1.5 bg-foreground/60" />` appended while `streaming`.
- **Tool call / response** — `Collapsible` with a dashed border header (`border-dashed bg-muted/40`) and monospace tool name. Lucide `Wrench` for call, `CheckCircle2`/`AlertTriangle` for response. Args/results render as `<pre>` inside `CollapsibleContent`.
- **Thinking** — italic dim text in a dashed block with `Sparkles` icon.
- **Agent label** — outline `Badge`, mono text, uppercase — only rendered when author *changes* between rows. Looks like a subtitle above the bubble.
- **System event** — centered muted line. Red `text-destructive` for errors. Keeps noise low.

Rule of thumb: if the content is verbose or debug-ish (tool args, tool results, reasoning), put it inside a `<Collapsible>` so the default view stays readable.

---

## 8. Data flow

```
ChatPage
  ├── useState<OrxEvent[]>(events)
  ├── useEffect: fetchSessionEvents on mount (rehydrate if refresh)
  ├── send(prompt):
  │     push local user event  →  for await streamEvents():  push each event
  └── <Transcript events={events} />
        └── reducer → rows → map over row renderers
```

No global store. `events` lives in `ChatPage`. Session id comes from the URL. That's the entire state model.

---

## 9. Vite dev proxy (don't use CORS)

```ts
server: {
  port: 5173,
  proxy: {
    "/api": { target: "http://127.0.0.1:8932", changeOrigin: true },
  },
}
```

Frontend always calls `/api/...`. No env vars, no CORS middleware, no prod/dev branching. In production you put a reverse proxy in front of both anyway.

**Use `127.0.0.1`, not `localhost`** in proxy target if the backend binds IPv4 only — macOS sometimes resolves `localhost` to IPv6 first.

---

## 10. Testability — `data-testid` everywhere

Every interactive element and every event-row kind has a `data-testid`:

```tsx
<div data-testid="user-message">...
<div data-testid="agent-text">...
<div data-testid="tool-call">...
<textarea data-testid="prompt-input" />
<Button data-testid="send-button" />
```

Makes Playwright tests and live-demo scripts trivial: `page.getByTestId('send-button').click()`. Accessibility roles alone aren't specific enough when multiple bubbles share the same role.

---

## 11. Dark mode

CSS vars already defined under `.dark`. Add a theme toggle by flipping the class on `document.documentElement`. Persist with `localStorage`. Don't install a theme library — it's ~15 lines.

---

## 12. Scaffold checklist (for a Skill)

1. `vite + react-ts` template, rename to kebab-case.
2. Install: `react-router-dom uuid lucide-react clsx tailwind-merge class-variance-authority tailwindcss-animate react-markdown remark-gfm @radix-ui/react-collapsible @radix-ui/react-scroll-area @radix-ui/react-separator @radix-ui/react-slot`.
3. Dev: `tailwindcss postcss autoprefixer @types/uuid @types/node`.
4. Add `tsconfig` path alias `@/* → ./src/*` + Vite alias to match.
5. Tailwind config with shadcn neutral vars + `darkMode: ["class"]` + `tailwindcss-animate` plugin.
6. `index.css` with shadcn `:root` / `.dark` vars + `.markdown` styles.
7. `lib/utils.ts` with `cn()`.
8. Drop in the shadcn primitives you need (only what you use — `button`, `card`, `textarea`, `collapsible`, `badge`, `scroll-area`, `separator` for this style of UI).
9. `SiteLayout` with header + `<Outlet />`.
10. Stub a `/` config/index page and a `/session/:id` detail page.
11. Vite proxy for `/api`.
12. Stream helper (`lib/stream.ts`) — copy verbatim, it's not worth rewriting.

The rest is use-case-specific: adapt the row renderers, keep the reducer pattern, keep the testid convention.
