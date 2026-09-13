# driving a portal with the browser

The headless browser exists for counterparties that only offer a website: government filing
(IRS Direct Pay, state portals), vendor ordering (a distributor's site), carrier pages. The loop
is `browse_open` → read the page as a labeled tree → `browse_fill` / `browse_click` →
`browse_snapshot` to see what changed.

## before you drive an unfamiliar portal

`search_guides` and `get_standard` — another gerp may have recorded the exact steps for this
portal. If you had to work one out yourself, contribute what you learned back, so the next firm
doesn't repeat it.

## filling

Fill only values the owner gave you or that come from the books — never invent a field's
contents to get past a form. A portal login is a vaulted secret: `collect_secret` it once, then
fill the field with `secret:<name>`; the password resolves inside the tool and never passes
through the chat.

## the submit boundary

**Never click a final submit that moves money or files anything unless the owner approved that
specific submission in this conversation.** Stop at the review step, `browse_screenshot` it, and
show them. Approval for one submission is not approval for the next.

After a completed flow, screenshot the confirmation — it files as a document, and offer to email
it. If a page wedges, `browse_close` and start clean rather than clicking at a stuck tree.
