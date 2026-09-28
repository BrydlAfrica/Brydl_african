# Datasets — starter CSVs for Brydl projects

Import these from Admin → your project → **Tasks → Upload CSV**. The
importer expects exactly two columns: `text` (required) and an optional
`gold` (the reference answer, used for QA training — leave blank when
the task has no gold). CSVs here are safe to drop into the importer as
is.

## english_to_pidgin_starter.csv

50 hand-checked English → Nigerian Pidgin pairs across everyday
domains (food, market/money, transport, family, work/school, health,
tech/utility — NEPA, generator, airtime, suya, jollof, akara, eba,
Third Mainland Bridge all make appearances).

Use this as your **gold reference** for QA (upload with the `gold`
column) OR as your seed batch (strip the gold column before upload if
you want clean annotator output without revealing the answer).

To scale up, mix in pairs from the open sources below.

## Open Nigerian-Pidgin datasets to grow from (all free, permissive licenses)

| Source | Pairs/utterances | License | Notes |
| --- | --- | --- | --- |
| **NaijaSenti** (Hausa NLP group) | ~80k Nigerian-Pidgin tweets, sentiment-labeled | MIT | https://github.com/hausanlp/NaijaSenti — filter to the English↔Pidgin code-mixed rows for back-translation pairs |
| **Lafand-MT** (Masakhane / AI4D) | ~18k English↔Nigerian-Pidgin parallel sentences | CC-BY-4.0 | https://huggingface.co/datasets/masakhane/lafand-mt — clean train/valid/test splits, ready to convert to our CSV format |
| **NollySenti** | Nollywood reviews, code-mixed | research use | Good for colloquial / expressive pidgin |
| **JW300 en-pcm** | ~38k parallel sentences from Jehovah's Witnesses | CC-BY-NC | Religious domain; useful for formal-register pairs, but mix with colloquial |
| **OPUS en-pcm** (Open Parallel Corpus) | varies | mixed | Aggregated parallel text — filter quality before import |
| **BBC Pidgin** web crawl | millions of sentences | copyright news | For language modeling, not direct parallel pairs |

## Recipe for building a real training set

1. Import 100–200 gold pairs (50 from starter.csv + 50 hand-picked from
   Lafand-MT) as a translation project WITH gold → use these to
   calibrate your QA reviewers on what good pidgin looks like.
2. Upload 500 English sentences WITHOUT gold → assign to 2 annotators
   each → run consensus; disagreements go to arbitration.
3. Pay particular attention to:
   - **Code-switching**: annotators should not force pure Pidgin when
     English words (bus, ATM, traffic, internet, battery) are what
     Nigerians actually use.
   - **Dialect consistency**: Warri vs Lagos vs Onitsha Pidgin vary —
     write a short style guide in the project instructions so the team
     converges on one variant (Lagos Pidgin is the safest default).
   - **Nigerian entities** (NEPA, Naira, kobo, Nollywood, suya, akara,
     okada, danfo) must NOT be "translated" to generic English.
4. Once you have ~2k approved pairs you can export via the client
   portal JSONL and fine-tune a small model (LLaMA or Mistral 7B via
   LoRA gets you 90%+ quality on Pidgin translation from that volume).
