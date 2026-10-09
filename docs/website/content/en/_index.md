---
title: "MultiGPT"
description: "Self-hosted multi-AI chat system for families and households: your own data, many providers, one chat."
eyebrow: "Self-hosted · early stage"
headline: "One AI chat for the whole family – on your own server"
lead: "MultiGPT is meant to become a web app for your home network that everyone in the household uses to chat with different AI models – from OpenAI, Anthropic and Google to a local LM Studio. Chats, accounts and API keys stay on your own server."
pillarsTitle: "The idea behind it"
pillarsLead: "These are the goals for version 1. The status labels further down show what has been built so far."
pillars:
  - title: "Your own data"
    text: "The interface, chat histories and API keys live only on your own server in your home network, in PostgreSQL. Keys are stored encrypted. The only traffic leaving the network is the requests to the AI providers you choose."
  - title: "One chat for many providers"
    text: "One interface for all connected models, with the model chosen per message. Local models from LM Studio join in for free whenever that PC is on."
  - title: "Family accounts with roles and budgets"
    text: "Every family member gets their own account. Roles define who may use which models and features. Monthly budgets keep costs in check."
menus:
  main:
    name: "Home"
    weight: 1
---

## Who is it for?

For technically minded people who want to give their family or household access to AI
models without a separate subscription for each person and without collecting all chats
at one provider. MultiGPT is meant for a server that is running anyway – a NAS or a small
home server with Debian – and that is reachable only from the home network.

## What exists today

To be honest: not much to chat with yet. What is done is the foundation from milestone 1 –
a Django project with login, login throttling, an empty chat page, a health check, a Debian
package and a systemd unit. What comes next is in the [roadmap]({{< relref "roadmap" >}}).

## The tech in one sentence

Python and Django 5.2 behind gunicorn, PostgreSQL with pgvector as the only database,
an interface built from Django templates and a little vanilla JavaScript, no Node build
step, no CDNs. More under [architecture]({{< relref "architecture" >}}).

*Note: the application's user interface itself is in German.*
