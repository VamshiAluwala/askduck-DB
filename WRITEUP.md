# Approach and Decisions

**Live:** https://darwinbox-task.streamlit.app · **Code:** https://github.com/VamshiAluwala/askduck-DB


## How I scoped it

The brief asks for a "clear, **correct** answer." I kept coming back to that second word. Hand
an LLM a few thousand rows and ask for a total and you get a number that looks right and often
isn't, and it gets worse as the file grows.

So I split the job. The model writes SQL, DuckDB runs it, and the model never touches a number.
Everything else followed from that. It also made cross-file analysis nearly free: once every
upload is a table in the same database, answering across files is just a `JOIN`.

## What I chose, and why

**Streamlit + DuckDB + pandas, one file.** The budget was 4–6 hours. A React/FastAPI split would
have spent half of it on plumbing nobody is grading.

**The model sees the schema, never the data.** Column names, SQL types, row counts, three sample
rows. The prompt stays around 1,000 characters whether the file has 150 rows or a million.

Two fixes there came out of testing, not planning. I was describing columns with pandas dtypes;
shown `datetime64[us]` the model called trend questions unanswerable, and shown `TIMESTAMP` it
reached for `date_trunc` immediately. Same data, different label. And with only three sample rows
visible it once insisted a category wasn't in the file when it plainly was, so now any small
categorical column gets its full value list in the prompt. Both changes are a few lines. Both
moved answers from wrong to right.

**Join keys are computed, not guessed.** The app looks for columns that share a name, overlap in
real values, and are near-unique on at least one side. I got to all three the hard way. Name
matching alone invents joins on generic `id` columns. Load-testing at a million rows then showed
my overlap check was only comparing the first 500 distinct values per column, so two tables that
joined perfectly scored zero and the hint disappeared exactly where it mattered most; moving that
count into DuckDB fixed it and ran faster. Uniqueness came last — without it a shared category
like `leave_type` overlaps perfectly and gets offered as a key, fanning rows out instead of
linking them. Three tables were producing twelve suggestions, three of them real.

**One retry, not a loop.** A failed query goes back to the model once with the engine's error.
Most failures are near-misses it fixes immediately, and an open-ended agent loop just burns time
on the ones it never will.

**Refusing beats guessing, but over-asking is its own failure.** Out-of-scope questions return
`CANNOT_ANSWER`, undefined ones ask back. My first prompt interrogated the user about "compare
revenue by segment" — an ordinary question with an obvious reading — so it's now biased toward
answering, and clarifies only when nothing names a metric at all.

**Open weights.** GPT-OSS 120B, served by Groq — Apache-2.0, so the `openai/` in the model id
names the model family, not a hosted OpenAI service. I built against Qwen 2.5 Coder on a local
Ollama and moved to Groq only because a cloud runner can't reach a laptop. Nothing in the app
knows which provider it's talking to, which is what kept that a config change rather than an
architectural one — and the local setup still works by changing one variable.

AI tools wrote most of the code. The judgment above, and the three bugs I only found by testing,
are the part I own.

## What I'd build next

1. **A verification pass** — re-ask the model whether the result actually answers the question.
   The biggest reliability win left.
2. **Messy real files** — junk rows above the header, merged cells, multi-row headers. Common in
   HR exports, and the loader assumes row 1 is the header.
3. **A semantic layer** — a per-customer glossary mapping "attrition" or "active headcount" to real
   columns. This is where a Darwinbox deployment would actually live: same engine, different
   vocabulary.
4. **Read-only guardrails** — reject anything that isn't a `SELECT`, cap result size. Trivial now,
   necessary the moment this points at a real warehouse.
