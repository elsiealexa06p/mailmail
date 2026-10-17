# antibot

Self-hosted bot mitigation gate. Sits between an email/redirect link and its
real destination, scores each request with layered signals, and lets humans
through while quietly blocking or challenging bots.

## Deploy (fresh Debian 12 / Ubuntu 22.04+ box)

Der Code lebt als `antibot/` Subdir im **badbodloves/mailer** Repo. Der Installer
holt sich das ganze Repo, benutzt aber nur das Unterverzeichnis.

```bash
apt install -y curl
curl -fsSL https://raw.githubusercontent.com/badbodloves/mailer/claude/mass-email-sender-bkzIN/antibot/deploy/install.sh \
    | DOMAIN=xyz.deinedomain.de bash
```

Then open `https://xyz.deinedomain.de` — a wizard walks you through:

1. Admin user + password
2. HMAC secret generation (copy this into your mailer's `/antibot-config` page)
3. Default redirect target (fallback when a token doesn't carry one)
4. Logo + wait-text + primary color (shown during silent challenge)
5. Optional: Turnstile / MaxMind / webhook keys

## Flow

```
recipient clicks link in email
  → https://xyz.deinedomain.de/go/<hmac-token>
  → scoring engine (ASN, UA, honeypot, PoW, verification cookie…)
  → score ≤ allow-threshold → 302 to real target
  → allow < score < block   → silent challenge, then verify → 302
  → score ≥ block-threshold → 403 or honeypot
```

The mailer signs the token with the shared HMAC secret; only requests carrying
a valid token get through the gate at all. Random visitors: 404.

## Update

As the `antibot` user (SSH-key):

```bash
bash ~/mailer/antibot/deploy/update.sh
```

## Layers

| # | Layer      | Signal                                                         |
| - | ---------- | -------------------------------------------------------------- |
| 1 | Network    | ASN (cloud/hosting → +40), country baseline, rate-limit bucket |
| 2 | Client     | navigator.webdriver, WebGL vendor, plugins, canvas hash        |
| 3 | Behavior   | Honeypot field, submit timing, mouse-move budget               |
| 4 | Challenge  | Altcha PoW (silent), optional Turnstile fallback               |
| 5 | Persistence| Signed verification cookie (6h TTL) — human doesn't re-solve   |

## Dry-run

The wizard turns **dry-run on by default**. Every decision is logged as if it
were enforced, but nothing is actually blocked. Watch the log for a day, tune
your thresholds, then flip dry-run off in Settings.
