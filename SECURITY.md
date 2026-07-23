# Security Policy

## Supported versions

Security fixes are provided for the latest published release. This project is currently alpha software; upgrade to the newest patch release before reporting an issue.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or leaked credential. Use GitHub's **Report a vulnerability** form under the repository's Security tab. Include affected versions, reproduction steps, impact, and any suggested mitigation. Maintainers should acknowledge a complete report within seven days.

If private vulnerability reporting has not yet been enabled for the repository, contact a maintainer privately and disclose only enough information to establish a secure reporting channel.

## Deployment boundary

Trade Compass Agent is a local-first, single-user application. The current release accepts only loopback bind targets such as `127.0.0.1` and `::1`; remote listening is intentionally unsupported. The legacy HTTP callback route fails closed until platform-specific signature and replay verification are implemented. Bidirectional messaging uses authenticated platform gateway connections.

The project performs paper trading and analysis only. It does not place orders with a real broker. API keys and webhook secrets belong in the local `.env` file and must never be committed.
