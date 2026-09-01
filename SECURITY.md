# Security policy

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability. Use GitHub's
private vulnerability reporting for this repository:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Include the affected version, impact, reproduction, and any proposed fix.

Do not include live API keys, captured credentials, private URLs, or personal data.
Use synthetic values in reproductions.

## Supported versions

Security fixes are made against the latest published release. Older releases may
receive a fix only when the maintainers explicitly announce one.

## Security boundary

The server validates caller-supplied fetch URLs and guards browser redirects before
returning content. Application-level checks cannot guarantee that no packet reaches
a private address during DNS rebinding; deployments with that threat model need a
validating egress proxy or equivalent network policy. HTTP transport has no built-in
authentication and should remain loopback-only unless an authenticated perimeter is
provided.
