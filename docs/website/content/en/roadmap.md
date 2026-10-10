---
title: "Roadmap"
description: "MultiGPT's milestones and their status."
lead: "MultiGPT is built milestone by milestone. After each milestone the tests run, there is a short report, and then work continues. As of version 0.3.1, milestones 1 to 8 including 4a are done, images (9) and operations (12) partly. Voice (10), music (11), the scratchpad (13) and the runner (14) are still open."
menus:
  main:
    weight: 40
---

{{< roadmap >}}

## Order

The critical path is **1 → 2 → 3 → 4 → 4a**: everything that uses tools (document search,
web search, calculations, PDF sheets, image generation and the runner) depends on the MCP loop.
Convenience (5) and usage/budgets (6) could run in parallel after milestone 3. HTTPS with nginx
already ships in the package and is therefore ready before the voice features (10).

For the scratchpad (13), the context menu and prompt templates come first. The runner (14)
builds on MCP, the billing accounts from milestone 6 and nginx with TLS; at first it offers
tools only, with no coding agent inside the container.

## Later (after version 1)

Full-text search across all messages and a "memory across chats" (proposal, not yet approved).
