# Specification: course discussions (content, media, pagination, moderation)

**Status:** Product / engineering specification. **Not implemented** in the codebase at the time of writing; the current UI stores plain text only and loads all posts in one view.

**Scope:** Discussion threads attached to **course materials** and **questions** (same product surfaces as today), including **optional Discussion AI** replies. Out of scope unless separately specified: site-wide forums, direct messages, notifications digest, full-text search.

**Audience:** Product owners, teachers expecting moderation tools, and implementers (including coding agents).

---

## 1. Goals

1. **Readable long threads:** Pagination (or equivalent chunking) so very large topics do not degrade the browser or database incidentally.
2. **Richer expression:** Support **Markdown** in human posts for formatting and inline images where safe.
3. **Trust and safety:** Sanitized rendering, sensible attachment limits, and **moderation** (delete posts, mute users in a course context).
4. **Admin configurability:** At least one **platform-managed** knob for pagination / performance (see section 4).

Non-goals for v1 of this spec: LaTeX math rendering in discussions (may be a follow-up), arbitrary file attachments beyond images, collaborative real-time editing.

---

## 2. Current baseline (for implementers)

Today, posts are a single `body_text` field, submitted via a plain `<textarea>`, rendered with HTML escape + line breaks (no Markdown, no uploads, no pagination). Any implementation should define **migration behavior** for existing rows (treat legacy bodies as Markdown-compatible plain text).

---

## 3. Authoring model

### 3.1 Input format

- **Authoring format:** **CommonMark-flavored Markdown** (exact dialect to be chosen at implementation time; document the chosen library and extensions in the change notes).
- **Allowed constructs (v1):**
  - Paragraphs, headings up to `###`, bold, italic, inline code, fenced code blocks, bullet and numbered lists, blockquotes, links.
  - **Images:** `![alt](url)` only when the URL passes the **URL policy** (section 5.2). No `data:` URLs for images in v1.
- **Disallowed or stripped in v1:**
  - Raw HTML blocks from authors (do not pass user HTML through to the DOM unescaped).
  - HTML inside Markdown where the renderer would normally allow it: normalize to “Markdown-only safe subset” or run through a sanitizer that removes tags not produced by the trusted Markdown pipeline.

### 3.2 Attachments (images uploaded with a post)

- **v1:** Support **image uploads** (e.g. PNG, JPEG, WebP, GIF) attached to a post, stored under the existing course data / upload discipline (path layout and access control to be aligned with `storage_paths` and course membership).
- **Limits (defaults, tunable later if needed):**
  - Max **N** images per post (recommended default: **4**).
  - Max file size per image (recommended default: **5 MB**, consistent in spirit with existing upload caps unless product says otherwise).
- **Embedding:** The editor may insert Markdown that references the uploaded file via a **stable, course-scoped URL** served by the app (not a bare filesystem path). Students must not guess URLs for others’ private uploads.

### 3.3 Discussion AI replies

- AI-generated bodies use the **same rendering pipeline** as human posts (Markdown → sanitize → HTML).
- AI must not emit links or images unless product explicitly enables that later; v1 may restrict the model instruction set to **no images and only whitelisted link domains** (e.g. course material links only).

---

## 4. Pagination and performance (admin-configurable)

### 4.1 Platform setting

- Introduce a **platform-level** configuration value (name illustrative: `discussion_posts_page_size`) **managed in the admin UI** and stored persistently (exact storage mechanism is an implementation detail).
- **Semantics:** Maximum number of **posts** (root + replies count as individual posts unless product chooses “root-only pages”—default below) returned for **one page** of the discussion view API or server-rendered page.
- **Recommended default:** **50** posts per page.
- **Allowed range (product guardrails):** e.g. **10–200**, inclusive, to avoid accidental `1` or `100000`.
- **Sorting:** Default **chronological ascending** by `created_at` within the topic (consistent with today’s mental model). Optional later: “newest first” toggle per course (out of v1 unless requested).

### 4.2 Pagination UX

- **Cursor or offset:** Implementation may use offset-based paging for simplicity or keyset/cursor paging for stability under concurrent inserts; the spec requires **stable ordering** and no duplicate rows across pages when moving forward.
- **UI:** Clear “older posts” / “newer posts” or numbered pages; deep-linking to a page or anchor (specific post id) is desirable for teachers sharing links—mark as **should** for v1, **must** if low effort.

### 4.3 Thread shape vs paging

- Default paging is **flat post list** in creation order (matches current “flat_thread_for_template” behavior conceptually).
- **Reply nesting display** may remain visual-only (indentation); paging still counts **each post row** toward the page size. (Alternative “threaded paging” is out of v1.)

### 4.4 Performance expectations

- Listing endpoints must not load unbounded post bodies for a topic in one query once this spec is in force.
- Consider an upper bound on **rendered HTML size** per post after sanitization; truncate or reject with a clear validation error (exact policy: implementation, default e.g. 256 KB per post body source).

---

## 5. Security and content safety

### 5.1 XSS and sanitization

- All rendered HTML must go through a **sanitization step** appropriate to the Markdown renderer output (allow-list tags/attributes for links and images).
- **Links:** `rel="noopener noreferrer"` on external links; optional `nofollow` for user-generated links (recommended **on** for v1).
- **Images:** In addition to Markdown parsing, enforce **HTTPS-only** image URLs for remote images, or restrict to same-origin uploaded files in v1 (simplest: **only same-origin uploads** for `![alt](url)` in v1, block arbitrary remote URLs).

### 5.2 URL policy (remote images and links)

- **v1 simple policy:** Remote `http://` and `https://` images in Markdown are **disabled**; users must use **uploads** for images.
- **Links** in Markdown: allow `https:` and `mailto:`; block `javascript:` and other dangerous schemes.
- **Future:** Admin-maintained allowlist domains for remote images (optional extension).

### 5.3 Anonymous posts

- Anonymous posts follow the **same** Markdown and attachment rules.
- Avatars remain suppressed for anonymous authors (current behavior). Uploaded images in an anonymous post must not embed metadata that reveals identity beyond what the author intentionally shows (implementation note: strip sensitive EXIF where feasible).

---

## 6. Moderation and permissions

### 6.1 Delete post

- **Course Teacher and TA:** May delete any post within discussions for courses where they hold that role (material or question topic belonging to that course).
- **Platform admin / super admin:** May delete posts in any course (break-glass moderation).
- **Students:** May not delete others’ posts; **optional v1:** allow delete **own** post within **edit window** (e.g. 15 minutes) — default for v1: **no self-delete** unless product insists (reduces “hit and run” abuse). If omitted, document as future.
- **Deletion kind:** **Hard delete** of post row and attachment files for v1; **soft delete** (tombstone “removed by moderator”) is a desirable follow-up for audit and transparency.

### 6.2 Mute (course-scoped)

- **Definition:** A **course-scoped mute** prevents the user from creating **new** posts and replies in **any** discussion topic in that course. It does not remove existing content unless moderators delete it separately.
- **Who can mute:** Same as delete (Teachers, TAs, platform admins for break-glass).
- **Duration:** v1 supports **permanent until unmuted** and **time-bounded** (admin/teacher sets end timestamp). Unmute by same role class.
- **Visibility:** Muted user sees a clear message when attempting to post. Non-muted users see no banner on the muted user’s past posts unless product adds “silenced” badges later (out of v1).

### 6.3 Audit

- **Should:** Log moderation actions (who deleted what, when; who muted whom) in an append-only admin-visible log or reuse an existing audit pattern if the product adds one later. Minimum for v1: persist enough in DB to answer support tickets (even if only `deleted_at`, `deleted_by_id` columns on posts).

---

## 7. Compatibility and migration

- Legacy `body_text` is interpreted as **Markdown plain text** (no syntax → renders as today visually).
- Old posts have **no attachments**; attachment tables optional for new content only.

---

## 8. Acceptance criteria (summary)

1. Admin can set **posts per page** within the configured min/max; changing the value affects newly loaded pages without server restart (if cached, define invalidation).
2. A post can include **Markdown** formatting that renders safely in the browser.
3. A post can include **uploaded images** within per-post and per-file limits; remote hotlinked images are **not** allowed in v1.
4. Long topics load in **pages**, not an unbounded single list.
5. Teachers/TAs (and platform admins) can **delete** posts in their scope; **mute** users for the course discussion as defined.
6. Anonymous and AI behaviors remain consistent with sections 3.3 and 5.3.

---

## 9. Open points (explicitly deferred)

- Self-delete window for authors.
- Soft-delete with visible tombstones.
- Remote image domain allowlist.
- Search within a topic.
- Email/notification on new replies.

When any deferred item ships, amend this document rather than relying on tribal knowledge.
