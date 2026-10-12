Entry point: `musclememory stdio`; Streamable HTTP is served at `/mcp` by
`musclememory serve`. Exactly five tool names use contract v1, advertised through
tool metadata as `musclememory/contractVersion`. The official SDK owns discovery,
protocol validation, transport framing, and MCP tool-error responses.
