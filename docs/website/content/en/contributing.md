---
title: "Contributing"
description: "How to get involved with MultiGPT."
lead: "MultiGPT is a small project still in development. Feedback, questions and contributions are welcome."
menus:
  main:
    weight: 50
---

{{< repo-info >}}

## How you can help

- **Try it and report back:** installing on a fresh Debian 13, the Docker route, the update
  from 0.2 to 0.3 and chats with real Anthropic and Gemini keys have hardly been tried in
  practice yet. Experience with them – especially on NAS systems – helps a lot.
- **Questions and ideas** as an issue: what would you need to use it in your own household?
- **Code:** the next steps are in the [roadmap]({{< relref "roadmap" >}}). Please open an
  issue before larger changes so we can coordinate.

## How the project is organised

- Planning and architecture are in `docs/Plan.md`, the work packages per milestone in
  `docs/Implementierung.md`.
- Development with `make`: `make install`, `make test` (pytest against PostgreSQL),
  `make lint` (ruff). Details under [installation]({{< relref "installation" >}}).
- The interface stays free of a Node build step and of CDNs. Provider adapters are written
  against the provider's current API documentation; tests run against mocked HTTP
  responses – never against real APIs.
- The application's interface and the project documentation are in German.

## License

{{< license-info >}}
