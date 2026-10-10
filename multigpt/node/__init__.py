"""Knoten (M15): MultiGPT als MCP-Server, gesteuert über API-Keys je Konto.

Externe Orchestratoren (n8n, Claude Desktop, andere Agenten) rufen MultiGPT mit
dem API-Key eines Kontos auf. Die Rechte ergeben sich aus dem Konto plus den
freigegebenen Rechten des Keys (``scopes``).
"""
