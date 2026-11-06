# Changelog

## 1.3.2

- Fix the missing move digest in the CLI, including `--sha256` and batch support.
- Preserve explicit preconditions unchanged for delete/move, reject invalid
  supplied digests instead of substituting a fresh one, and check the move
  receipt and full destination digest before reporting success.
- Serialize delete/move with write/append using the same locks, and hold both
  paths for move. Use native no-replace rename to prevent destination clobber.
- Use coordinated iCloud reads for destructive-operation digests on macOS.
- Separate audit entries with newlines while retaining serialized, best-effort logging.
- Add regression coverage for CLI, HTTP guards, concurrent writers, destination
  collisions, and audit entries. Tests operate on isolated temporary vaults.

The live n8n Move workflow must forward `expected_sha256` unchanged from its
incoming request body. This release does not automatically configure n8n.

Locks coordinate participating API writers, not arbitrary external editors or
iCloud. Filesystems without exclusive rename support fail closed. Write/append
preconditions remain optional for backward compatibility; null is not create-only.
Existing macOS path restrictions and the historical Linux HTTP entry point are
unchanged.
