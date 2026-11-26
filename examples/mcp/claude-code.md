# Claude Code MCP Setup

Register `code-indxr` globally for all repositories:

```bash
claude mcp add --scope user code-indxr -- code-indxr serve
```

Or add `.mcp.json` at the root of a project:

```json
{
  "mcpServers": {
    "code-indxr": {
      "command": "code-indxr",
      "args": ["serve"],
      "env": {
        "CODE_INDXR_OLLAMA_URL": "http://127.0.0.1:11434",
        "CODE_INDXR_EMBED_MODEL": "nomic-embed-text"
      }
    }
  }
}
```
