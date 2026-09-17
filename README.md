# Ask Your Spreadsheets

Upload CSV/Excel files, ask analytical questions in plain English, get correct answers and charts.

Built for the Darwinbox Forward Deployed Engineer take-home ([docs/REQUIREMENTS.md](docs/REQUIREMENTS.md)).

---

## The one design decision that matters

**The model writes the query. It never computes the answer.**

Ask an LLM to total a column and it will confidently produce a wrong number — quietly, and
worse as the file grows. So the model's only job here is translating a question into DuckDB
SQL against a real schema. **DuckDB executes it.** Every number on screen came out of a query
engine, not a language model, and the query is on screen next to it.

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
echo "OPENAI_API_KEY=sk-..." > .env    # or LLM_API_KEY for any other provider
streamlit run app.py
```

Opens at http://localhost:8501.

## Questions to try

These assume an HR-style dataset — the shapes generalise to any upload.

**Totals & averages**
- *How many employees do we have and what is the total annual payroll cost?* → metric
- *What is the average CTC by department?* → bar chart
- *How many leave days were approved per leave type?* → bar chart

**Cross-file (needs a join)**
- *Which departments have the highest average performance rating?* → employees + reviews
- *Do higher rated employees earn more? Show average CTC by rating.* → employees + reviews
- *Which employees took more than 15 leave days in 2024?* → employees + leave
- *What is the average rating of employees in Bengaluru?* → employees + reviews

**Trends & comparisons**
- *Show the monthly trend of leave requests in 2024* → line chart
- *Which location has the highest attrition rate?* → computed ratio
- *Compare average CTC between Engineering and Sales* → bar chart

**Filters & rankings**
- *List the top 5 highest paid employees with their department and designation*
- *Which employees joined in 2023 and are still active?*

**Where it should say no**
- *What is the weather in Mumbai tomorrow?* → `CANNOT_ANSWER`, no invented answer
- *Who is the best employee?* → `CLARIFY`, because "best" names no metric
- *What is everyone's home address?* → `CANNOT_ANSWER`, column does not exist


## Tech stack

| Piece | Choice | Why |
|---|---|---|
| UI | Streamlit | Upload widget, charts, dataframes free; hosts free on Community Cloud |
| Engine | DuckDB (in-memory) | Cross-file joins for free, fast on a laptop, zero infra |
| Loading | pandas + openpyxl | CSV and every Excel sheet, one table per sheet |
| Model | `gpt-4o-mini` (default), or any open-weight model via env vars | One OpenAI-compatible client; provider is three env vars |
| Config | python-dotenv | `.env` for keys, never committed |

## Swapping the model

Any OpenAI-compatible endpoint. Three env vars, no code change:

```bash
# OpenAI (current default)
OPENAI_API_KEY=sk-...

# Groq — open-weight Llama, fast
export LLM_BASE_URL=https://api.groq.com/openai/v1
export LLM_MODEL=llama-3.3-70b-versatile
export LLM_API_KEY=gsk_...

# OpenRouter — Qwen, DeepSeek, Mistral, etc.
export LLM_BASE_URL=https://openrouter.ai/api/v1
export LLM_MODEL=qwen/qwen-2.5-coder-32b-instruct
export LLM_API_KEY=sk-or-...

# Fully local via Ollama — no key needed
export LLM_BASE_URL=http://localhost:11434/v1
export LLM_MODEL=qwen2.5-coder:7b
export LLM_API_KEY=unused
```

> **On the open-source constraint:** the app is provider-agnostic by design — nothing in the
> code knows which model it is talking to, and the three env vars above switch it to open-weight
> Llama, Qwen or a fully local Ollama with no code change. The committed default is `gpt-4o-mini`
> because that is what I had a key for while building; the Groq and Ollama paths above are the
> open-weight configurations and are the intended production setup.

## Deploying

Push to GitHub → [share.streamlit.io](https://share.streamlit.io) → point at `app.py` → add
`LLM_API_KEY` under **Secrets**. Streamlit Cloud exposes secrets as environment variables, so
nothing in the code changes.

## How it works

```
upload ──▶ clean ──▶ DuckDB table          schema + sample rows + join keys
                          │                            │
question ─────────────────┴────────────────────────────┴──▶ open-weight model
                                                                  │
                                              SQL ◀───────────────┘
                                               │
                        execute ──▶ error? ──▶ feed error back, retry once
                                               │
                                            result ──▶ chart + table + the SQL
```

## Delta solutioning — what's on top of the raw model

The model call is about fifteen lines. These are the parts that make it trustworthy:

1. **Deterministic execution.** SQL, not arithmetic. Correct on 15 rows and on 15 million.
2. **Schema-aware prompting.** Real column names, dtypes and three sample rows go into the
   prompt — never the file. Costs stay flat as files grow, and the model stops inventing columns.
3. **The model sees SQL types, not pandas dtypes.** Columns are described from `DESCRIBE` —
   `TIMESTAMP`, `DOUBLE`, `VARCHAR`. Showing `datetime64[us]` instead made the model refuse
   "monthly revenue trend" outright, because it did not recognise the column as a date.
4. **Categorical columns list their distinct values.** A column with repeats and ≤15 distinct
   values ships its full value list; ID and free-text columns do not. Without this, three
   sample rows were the model's only evidence, and it answered "there is no information about
   SMB customers" for a segment that was right there in the data.
5. **Automatic join-key inference.** Before asking, the app finds columns that share a *name
   and actual values* across two tables and hands the model the join conditions. This is what
   makes cross-file questions work instead of silently returning one table's answer.
6. **Messy-data coercion on load.** `"$12,400.00"` → `12400.0`, `"Jan 15 2024"` → a real date,
   `Customer ID` → `customer_id`, duplicate headers de-duplicated. An 80%-parse threshold means
   a genuine text column stays text. Without this, `SUM(amount)` fails on the raw export.
7. **Self-repair on failure.** A query that errors goes back to the model with the engine's
   error text for exactly one retry — recovers the common near-misses without looping forever.
8. **Refusal and clarification.** Out-of-scope questions return `CANNOT_ANSWER`, genuinely
   undefined ones return `CLARIFY: <question>` — but the prompt is deliberately biased toward
   answering, since an app that interrogates you about every ordinary question is its own
   failure mode. "Compare revenue by segment" answers; "who is the best customer" asks back.
9. **The SQL is always visible.** Collapsed by default, expanded on failure. A wrong answer
   becomes debuggable in five seconds instead of being a black box.

## Scoped out on purpose

Auth, cross-session persistence, conversation memory, RAG, agent frameworks, Docker, CI.
