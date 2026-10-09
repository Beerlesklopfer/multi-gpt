---
title: "Roadmap"
description: "MultiGPT's milestones and their status."
lead: "MultiGPT is built milestone by milestone. After each milestone the tests run, there is a short report, and then work continues. So far milestones 1 to 8 are done, including 4a (MCP tools). Images (9), voice (10), music (11) and operations (12) are still open."
menus:
  main:
    weight: 40
---

{{< roadmap >}}

## Order

The critical path is **1 → 2 → 3 → 4 → 4a**: everything that uses tools (document search,
web search and image editing as tools) depends on the MCP loop. Convenience (5) and
usage/budgets (6) can run in parallel after milestone 3. HTTPS with nginx is set up before
the voice features (10).

## Later (after version 1)

Images as input to models, reusable prompt presets, full-text search across all messages.
