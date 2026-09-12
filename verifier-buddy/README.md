# Verifier Buddy

A fast email verifier for the terminal, powered by [MailTester Ninja](https://mailtester.ninja).
One file, no dependencies beyond Python 3.9+, and it remembers your API key.

```
 _    __          _ _____              ____            __    __
| |  / /__  _____(_) __(_)__  _____   / __ )__  ______/ /___/ /_  __
| | / / _ \/ ___/ / /_/ / _ \/ ___/  / __  / / / / __  / __  / / / /
| |/ /  __/ /  / / __/ /  __/ /     / /_/ / /_/ / /_/ / /_/ / /_/ /
|___/\___/_/  /_/_/ /_/\___/_/     /_____/\__,_/\__,_/\__,_/\__, /
                                                           /____/
```

## Install

```bash
bash verifier-buddy/install.sh        # puts `verifier` in ~/.local/bin
# or, system-wide:
bash verifier-buddy/install.sh --system
```

Open a new terminal and type `verifier`.

## Use

```bash
verifier                          # interactive: paste emails or a file path
verifier a@b.com c@d.com          # verify a few addresses
verifier leads.csv                # every address found in the file
verifier leads.csv -o clean.csv   # choose where results go
```

**First run** asks for your MailTester Ninja API key and which plan it's on
(that sets the request rate). **Every run after that** asks whether to keep
the saved key; answer `n` to paste a different one. The key is stored in
`~/.config/verifier-buddy/config.json` with owner-only permissions.

Results print live, colour-coded, and are saved to a CSV in the current
folder (`<input>-verified-<timestamp>.csv`) with columns
`email,status,code,message,mx`. Statuses:

| status       | meaning                                          |
|--------------|--------------------------------------------------|
| `valid`      | mailbox accepted (MailTester `ok` / Accepted)    |
| `invalid`    | rejected, or the domain has no MX (`ko`)         |
| `catch-all`  | the domain accepts anything; can't confirm       |
| `risky`      | disposable / temporary address                   |
| `unknown`    | busy / timed out / rate limited after retries    |
| `bad-syntax` | not an email address; never sent to the API      |

## Why it's fast and doesn't fall over

- Requests run in parallel (up to 64 threads) but are metered to your plan's
  limit as an even drip, never a burst, so the vendor's limiter is never tripped.
- HTTP 429 / "Limited" answers back off and retry; the gap between calls
  stretches on a 429 and relaxes again after a run of clean answers.
- Network hiccups and 5xx answers retry with keep-alive connections.
- Busy / timed-out mailboxes (`mb`, Timeout, Mx Error) are re-checked twice
  at the end instead of being written off as unknown.
- Duplicates and malformed addresses are dropped locally before any API call.
- Ctrl-C stops within a moment and still writes everything that finished.
- Both MailTester auth styles work: direct `key=` and the older token flow.

## Options

```
-k, --key KEY       use (and save) this API key
-r, --rate N        requests per 10 seconds (Starter 5, Pro 11, Ultimate 57)
-w, --workers N     concurrent requests (default: 3× rate, max 64)
-o, --output FILE   results CSV path
    --no-recheck    skip the second look at busy mailboxes
    --reset         forget the saved key and ask again
    --no-banner     skip the ASCII art
```

## Tests

```bash
python3 verifier-buddy/tests/test_verifier.py
```

They run the real CLI against a local fake MailTester server (auth, rate
limiting, retries, interactive prompts, Ctrl-C-safe output).
