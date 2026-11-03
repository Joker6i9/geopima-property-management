# Real M-Pesa: setup and go-live

The system takes M-Pesa money two ways, both through Safaricom's Daraja API:

1. **Paybill payments (C2B).** A tenant pays to the paybill from their phone. Safaricom calls our *confirmation* address; the payment is stored and applied to the tenant's oldest open invoices.
2. **PIN prompts (STK push).** A tenant (Pay rent page) or staff (tenant page > *Request M-Pesa payment*) asks for a prompt on a phone. When the PIN is entered Safaricom calls our *STK callback*; the payment is recorded and applied the same way.

A payment that arrives both ways (a prompt to a paybill also triggers a paybill confirmation) is recorded once, by M-Pesa receipt number.

## Environment variables (backend `.env`)

| Variable | What |
|---|---|
| `MPESA_ENV` | `sandbox` (default, no real money) or `production` |
| `MPESA_CONSUMER_KEY`, `MPESA_CONSUMER_SECRET` | From your app on developer.safaricom.co.ke |
| `MPESA_PAYBILL` | The paybill (shortcode). Sandbox: `174379` |
| `MPESA_PASSKEY` | Lipa na M-Pesa Online passkey (sandbox: on the app's Test Credentials page; production: issued by Safaricom on Go Live) |
| `MPESA_CALLBACK_SECRET` | A long random string. It is part of the callback addresses so only Safaricom knows them. `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `MPESA_PUBLIC_URL` | This backend's public **https** address, no trailing slash, e.g. `https://api.youragency.co.ke`. Safaricom must be able to reach it. |
| `MPESA_BASE_URL` | Optional. Overrides Safaricom's host (used only for testing against a fake). Leave empty. |

Settings > *M-Pesa connection* (admins) shows which of these are still missing, without ever showing a value.

## Steps

1. Create an app on developer.safaricom.co.ke and fill the variables above. Deploy the backend on https.
2. In the web app: Settings > M-Pesa connection > **Connect paybill to this system**. This registers the confirmation and validation addresses with Safaricom (`/mpesa/c2b/v2/registerurl`, response type *Completed*). Safe to repeat.
3. Sandbox check: Safaricom's simulator can push a test C2B payment; it should appear under Money > Payments. Send yourself a prompt from a tenant page with the sandbox test phone `254708374149`.
4. Go live: complete Safaricom's Go Live for the paybill, switch `MPESA_ENV=production`, replace the key, secret, passkey and paybill with the production ones, then press **Connect** again.

## Why the addresses look like `/api/v1/payments/c2b/<secret>/...`

Daraja rejects registered URLs containing words such as "mpesa" or "safaricom". The older `/api/v1/mpesa/c2b/...` addresses still work for anything registered before.

## Behaviour worth knowing

- Callbacks always get an acknowledgement; unknown receipts or malformed bodies are ignored and logged, never applied.
- A prompt's tenant is fixed when it is sent. The callback can't redirect the money to someone else.
- One prompt at a time per tenant (90 seconds). Amounts 1 to 150,000 KES.
- If a callback never arrives, checking the prompt asks Safaricom directly after 20 seconds, and the prompt lapses after 5 minutes. A prompt that *was* paid but whose callback was lost is still recorded by the paybill confirmation.
- Unmatched payments (wrong account number) still land under Money > Payments > *Unmatched* for manual assignment.

## Not covered yet

- Reversals/refunds through the API (refunds are still done in your bank and marked in the system).
- B2C payouts to landlords.
- Verifying that callbacks come from Safaricom's IP ranges (the secret address is the protection today; add an allow-list at your proxy if you want a second layer).
