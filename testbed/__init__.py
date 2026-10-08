"""Synthetic MCP server fleet shared by every workstream's tests.

* ``testbed.fleet``        — deterministic generator: N servers, realistic,
  deliberately overlapping tools across five domains, with ground truth
  (domain / operation / duplicate group) for routing and dedup evaluation.
* ``testbed.servers``      — turns a ``ServerSpec`` into a real ``MCPServer``.
* ``python -m testbed.stdio_server`` — one fleet member over stdio.
* ``python -m testbed.serve``        — N fleet members over streamable HTTP.
* ``python -m testbed.seed``         — register + discover a fleet into the DB.
"""
