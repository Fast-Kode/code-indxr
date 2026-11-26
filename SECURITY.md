# Security Policy

## Reporting a Vulnerability

Please **do not** report security vulnerabilities through public GitHub issues.

Instead, use [GitHub Security Advisories](https://github.com/samarthakv/code-indxr/security/advisories/new) on this repository to report vulnerabilities privately.

Include:
- A description of the issue and potential impact
- Steps to reproduce or a minimal proof of concept
- Any affected versions or OS platforms

## Scope Notes

`code-indxr` is a local-only tool that communicates exclusively with the user's configured local Ollama endpoint (`CODE_INDXR_OLLAMA_URL` / `OLLAMA_HOST`) and reads/writes only the target repositories and local `CODE_INDXR_DATA_DIR`.
