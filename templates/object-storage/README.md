# Object storage

Creates a private, versioned S3 bucket with SSE-S3, TLS-only bucket policy, and all four public access block settings. Force destroy is off by default and never enabled for production. Empty a non-empty bucket and versions before deletion, or explicitly enable nonproduction force destroy.
