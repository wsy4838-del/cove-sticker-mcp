# Public-source privacy review

Reviewed for the first public GitHub push on 2026-09-08.

- The publication copy preserves the 17 implementation commits, with author
  and committer identities replaced by the public GitHub account and its
  GitHub noreply email. The original local repository is not published.
- All reachable commit and file objects were checked for personal filesystem
  paths, private contact details, credentials, private context and binary media.
- Gitleaks 8.30.1 scanned the full history and publication directory with no
  findings. Its official archive checksum was verified before execution.
- The wheel and source distribution were inspected: no personal image library,
  database, environment file, API key file or private local path is included.
- Runtime data, credentials, backup ZIPs and metadata manifests, lock files
  and hidden temporary files are excluded by `.gitignore`, including when a
  user explicitly points the data directory inside the source checkout.
- The source test suite passed 39 tests; lint and whitespace checks passed.
- The renamed package was installed into an isolated environment with normal
  runtime dependency resolution; the new and compatibility CLI names work.

This is a scoped review of the published source and artifacts, not a guarantee
about images or credentials a future user imports. Optional vision tagging
sends image previews to the configured provider. MCP clients receive selected
images and metadata. Original image resources and backups preserve original
bytes, including any embedded metadata. Provider keys are excluded from
library backups and API responses.
