# Datasets — starter CSVs for Brydl projects

Import any of these from Admin → your project → **Tasks → Upload CSV**.
The importer expects:
- header row with a `text` column (required)
- optional `gold` column (reference answer for QA training)
- plain CSV, UTF-8, one sentence per row.

Files:

## english_to_pidgin_starter.csv
50 hand-checked English → Nigerian Pidgin gold pairs across everyday
domains. Use this as a gold reference to calibrate your QA reviewers,
or delete the `gold` column before upload if you want open translation.

## english_pidgin_fintech_1100.csv
**1,100 English fintech/customer-service sentences, NO gold column —
ready for your English→Pidgin translation project.**

Sources and cleaning:
- ~60% from PolyAI/banking77 (CC-BY-4.0), filtered to banking-relevant
  categories, UK/Europe-specific language removed ("postcode", "euro",
  "Apple Pay"), "credit card" normalised to "debit card", length capped
  at 22–150 chars, ASCII-safe, deduped.
- ~20% curated Nigerian-digital-banking phrases covering USSD, BVN,
  NIP transfers, POS agents, Opay/PalmPay/Moniepoint/Kuda/Flutterwave,
  stamp duty, COT charges, virtual cards, quick loans, Gotv/DStv/IKEDC
  bills, WAEC pins, tiered KYC — the entities your annotators actually
  encounter.
- ~20% template paraphrases ("Abeg help me…", "How I go take…") to seed
  natural Pidgin register rather than stiff English-literal output.

Category balance (rows match multiple categories):
- transfer/send 192, card/ATM/POS 226, wallet/balance 122
- PIN/security/BVN 139, USSD/mobile app 214, fees/rates/limits 203
- customer care 164, bills/airtime 75, account management 75,
  loan/savings 55
- 106 rows explicitly name Nigerian entities (banks, wallets, bills).

### How to use this for English→Pidgin
1. Create a Project → task type **translation**, pay rate e.g. $0.08/task
   (fintech sentences are short — 60 chars avg — that rate is fair).
2. Write project instructions covering your Pidgin standard:
   - **Lagos Pidgin** as default (most widely understood).
   - Keep code-mixed English words that Nigerians actually use (POS,
     ATM, OTP, BVN, transfer, app, alert, PIN, USSD, KYC, Naira, kobo,
     Flutterwave) — DO NOT force-purify them.
   - Translate numbers naturally ("five hundred naira" → "five hundred
     naira", not a literal replacement).
   - Customer-service tone stays polite but conversational ("Abeg",
     "please", "o" where natural).
3. Upload this CSV (it has no gold column, so annotators see clean
   English prompts).
4. Set consensus RLHF with 2 annotators per task → disagreements route
   to QA for arbitration.
5. After ~500 approved pairs, you have enough for a LoRA fine-tune on
   Mistral/LLaMA 7B that handles fintech Pidgin at usable quality.

## Scaling further
- **Lafand-MT** (Masakhane, CC-BY-4.0) — ~18k parallel en↔pcm pairs,
  HuggingFace `masakhane/lafand-mt`. General domain; filter to finance
  keywords for more banking-specific pairs.
- **NaijaSenti** (HausaNLP, MIT) — Pidgin sentiment tweets for
  expressive/colloquial register.
- **JW300 en-pcm** — ~38k formal-domain pairs (CC-BY-NC; mix carefully).
- **OPUS en-pcm** — aggregated parallel text; filter quality.
- **BBC Pidgin** — monolingual corpus for language modeling.

All datasets in this folder are free for research and commercial use
under their upstream licenses (CC-BY-4.0 for banking77 + your own
work). No scraping of live bank or fintech customer data.
