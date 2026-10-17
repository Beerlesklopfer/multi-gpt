---
title: "Roadmap"
description: "MultiGPT's milestones and their status."
lead: "MultiGPT is built milestone by milestone. After each milestone the tests run, there is a short report, and then work continues. As of version 0.4.0, milestones 1 to 8 including 4a, the runner (14, with a few open points) and the node (15) are done, images (9), operations (12) and the scratchpad (13, so far the context menu and storage of files from tools) partly. Each milestone lists its work packages with their status. Voice (10) and music (11) are still open."
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

For the scratchpad (13), the context menu came first (0.3.2); prompt templates are next. The
runner (14) builds on MCP, the billing accounts from milestone 6, nginx with TLS and the
node (15): its tools run through the chat's MCP loop, and agents too reach it only through a
MultiGPT API key, never directly.

## Later (after version 1)

Full-text search across all messages and a "memory across chats" (proposal, not yet approved).
