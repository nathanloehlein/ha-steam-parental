"""Steam Web API access, with no Home Assistant imports.

Kept free of `homeassistant` so it can be imported and tested on its own, and
driven from `scripts/steam/probe.py` outside HA entirely. The component's own
`__init__.py` pulls in voluptuous and the rest; this does not.
"""
