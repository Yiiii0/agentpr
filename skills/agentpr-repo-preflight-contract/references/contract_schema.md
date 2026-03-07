# Contract Schema

Return one JSON object:

```json
{
  "status": "ready | skip | needs_review",
  "repo": {
    "owner": "string",
    "name": "string",
    "base_branch": "string"
  },
  "rules": {
    "commit_format": "string",
    "pr_checklist": ["string"],
    "must_update_docs": "true if CONTRIBUTING/PR template requires doc updates (default true — err on side of requiring docs)",
    "forbidden_changes": ["string"]
  },
  "toolchain": {
    "install": ["string"],
    "test": ["string"],
    "lint": ["string"],
    "env": ["string"]
  },
  "integration_plan": {
    "approach": "A | B | C | D",
    "target_files": ["string"],
    "expected_max_changed_files": 4,
    "expected_max_added_lines": 120
  },
  "blockers": ["string"],
  "evidence": {
    "sources": ["path or command"]
  }
}
```
