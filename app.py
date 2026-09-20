"""Ask questions about your spreadsheets in plain English.

The model writes SQL. DuckDB computes the answer. The model never does arithmetic.
"""
import os
import re
import textwrap

import duckdb
import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from groq import Groq, GroqError

load_dotenv()

# --- model config -----------------------------------------------------------
# GPT-OSS 120B on Groq: open weights (Apache-2.0), ~1s per answer. The key comes
# environment locally and from Streamlit secrets when deployed; swap LLM_MODEL
# for any other model Groq serves without touching the code.
def setting(name: str, default: str = "") -> str:
    """Environment first, then Streamlit secrets, then the default."""
    if value := os.getenv(name):
        return value
    try:
        return st.secrets[name]
    except Exception:  # no secrets file, or key absent
        return default


MODEL = setting("LLM_MODEL", "openai/gpt-oss-120b")
API_KEY = setting("GROQ_API_KEY") or setting("LLM_API_KEY")

SYSTEM = """You translate questions into DuckDB SQL over the tables described below.

Rules:
- Reply with ONE SQL query and nothing else. No prose, no markdown fences, no explanation.
- Use only the tables and columns given. Never invent a column.
- Quote identifiers with double quotes when needed.
- Prefer readable column aliases in the output.
- Date and timestamp columns support date_trunc, extract and strftime. A question about
  trends, months, quarters or "over time" is answerable whenever a date column exists.
- Reply CANNOT_ANSWER: <one sentence why> only when the data genuinely is not there — no such
  column, or the question is about something outside these tables entirely.
- Prefer the obvious reading over asking. "Revenue", "sales" and "spend" mean SUM of the
  amount-like column; "compare X and Y" means group by that column and total it.
- Reply CLARIFY: <one short question> only when no defensible default exists — "best",
  "top performer", "how are we doing" name no metric at all.
- Never use CANNOT_ANSWER for a value you simply did not see in the sample rows; the listed
  distinct values are authoritative.
"""

MESSY = re.compile(r"[₹$€£¥,%\s]")


# --- loading & cleaning -----------------------------------------------------
def slug(name: str) -> str:
    """Filename or header -> safe SQL identifier."""
    s = re.sub(r"\W+", "_", str(name).strip().lower()).strip("_")
    return s if s and not s[0].isdigit() else f"t_{s}"


def dedupe(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out = []
    for n in names:
        seen[n] = seen.get(n, 0) + 1
        out.append(n if seen[n] == 1 else f"{n}_{seen[n]}")
    return out


def is_text(s: pd.Series) -> bool:
    """True for object and pandas-3 StringDtype columns alike."""
    return s.dtype == object or pd.api.types.is_string_dtype(s)


def coerce_numeric(s: pd.Series) -> pd.Series:
    """'$1,240.00' -> 1240.0, but leave real text alone."""
    mask = s.notna()
    if not mask.any():
        return s
    cleaned = s.astype(str).str.replace(MESSY, "", regex=True).replace({"": None, "-": None})
    if pd.to_numeric(cleaned[mask], errors="coerce").notna().mean() < 0.8:
        return s
    return pd.to_numeric(cleaned, errors="coerce")


def coerce_dates(s: pd.Series) -> pd.Series:
    try:
        parsed = pd.to_datetime(s, errors="coerce", format="mixed")
    except (ValueError, TypeError):
        return s
    return parsed if parsed.notna().mean() >= 0.8 else s


def clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = dedupe([slug(c) for c in df.columns])
    for c in df.columns:
        if is_text(df[c]):
            df[c] = coerce_numeric(df[c])
        if is_text(df[c]):
            df[c] = coerce_dates(df[c])
    return df


def load(upload) -> dict[str, pd.DataFrame]:
    """One upload -> {table_name: df}. Excel workbooks yield one table per sheet."""
    stem = slug(upload.name.rsplit(".", 1)[0])
    if upload.name.lower().endswith(".csv"):
        return {stem: clean(pd.read_csv(upload))}
    sheets = pd.read_excel(upload, sheet_name=None)
    if len(sheets) == 1:
        return {stem: clean(next(iter(sheets.values())))}
    return {f"{stem}_{slug(name)}": clean(df) for name, df in sheets.items()}


def register(con, name: str, df: pd.DataFrame) -> None:
    con.register("_staging", df)
    con.execute(f'CREATE OR REPLACE TABLE "{name}" AS SELECT * FROM _staging')
    con.unregister("_staging")


# --- context the model sees -------------------------------------------------
def distinct_values(s: pd.Series, cap: int = 15) -> str | None:
    """Categorical column -> its full value list. IDs and free text -> None."""
    if not is_text(s):
        return None
    values = pd.unique(s.dropna())
    if 0 < len(values) <= cap and len(values) < len(s):  # repeats exist => categorical
        return ", ".join(repr(str(v)) for v in values)
    return None


def schema_text(con, tables: dict[str, pd.DataFrame]) -> str:
    """Real DuckDB types, not pandas dtypes — the model writes SQL, so show it SQL."""
    blocks = []
    for name, df in tables.items():
        described = con.execute(f'DESCRIBE "{name}"').df()
        lines = []
        for col, sql_type in zip(described.column_name, described.column_type):
            values = distinct_values(df[col]) if col in df else None
            lines.append(f"    {col} {sql_type}" + (f"  -- one of: {values}" if values else ""))
        sample = df.head(3).to_csv(index=False).strip()
        blocks.append(
            f"TABLE {name} -- {len(df)} rows\n"
            + "\n".join(lines)
            + f"\n  sample rows:\n{textwrap.indent(sample, '    ')}"
        )
    return "\n\n".join(blocks)


def join_hints(con, tables: dict[str, pd.DataFrame]) -> list[str]:
    """Columns that look like a key shared by two tables.

    Two tests, both needed. Values must overlap: a name match alone invents
    joins on generic columns like `id`. And the column must be near-unique on
    at least one side: a shared category such as `leave_type` or `department`
    overlaps perfectly in every table that has it, yet joining on it fans rows
    out instead of linking them. Uniqueness is what separates a key from a
    label, and only the key belongs in the prompt.

    The overlap is computed in DuckDB over the full columns. Sampling the first
    N distinct values in pandas looked equivalent and was not: on a large table
    the head of one column and the head of the other need not intersect at all,
    so real joins went undetected exactly when they mattered most.
    """
    hints, names = [], list(tables)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            for col in sorted(set(tables[a].columns) & set(tables[b].columns)):
                try:
                    na, nb, shared = con.execute(f'''
                        WITH x AS (SELECT DISTINCT "{col}" v FROM "{a}" WHERE "{col}" IS NOT NULL),
                             y AS (SELECT DISTINCT "{col}" v FROM "{b}" WHERE "{col}" IS NOT NULL)
                        SELECT (SELECT count(*) FROM x),
                               (SELECT count(*) FROM y),
                               (SELECT count(*) FROM x JOIN y USING (v))
                    ''').fetchone()
                except duckdb.Error:  # same name, incompatible types — not a join key
                    continue
                if not (na and nb) or shared / min(na, nb) <= 0.3:
                    continue
                unique = max(na / max(len(tables[a]), 1), nb / max(len(tables[b]), 1))
                if unique > 0.3:  # a key identifies rows; a category repeats across them
                    hints.append(f"{a}.{col} = {b}.{col}")
    return hints


def extract_sql(reply: str) -> str:
    fenced = re.search(r"```(?:sql)?\s*(.*?)```", reply, re.S)
    return (fenced.group(1) if fenced else reply).strip().rstrip(";")


# --- ask --------------------------------------------------------------------
def ask(question: str, tables: dict[str, pd.DataFrame], con):
    """-> (sql, message, result_df). Exactly one of message/result_df is set."""
    hints = join_hints(con, tables)
    context = schema_text(con, tables)
    if hints:
        context += "\n\nLikely join keys:\n" + "\n".join(f"  {h}" for h in hints)

    client = Groq(api_key=API_KEY)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"{context}\n\nQuestion: {question}"},
    ]

    sql = error = None
    for _ in range(2):  # one repair attempt, then give up
        reply = client.chat.completions.create(
            model=MODEL, messages=messages, temperature=0
        ).choices[0].message.content
        if reply.lstrip().startswith(("CANNOT_ANSWER", "CLARIFY")):
            return None, reply.strip(), None
        sql = extract_sql(reply)
        try:
            return sql, None, con.execute(sql).df()
        except Exception as e:  # noqa: BLE001 - surface any engine error to the model
            error = e
            messages += [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": f"That query failed:\n{e}\nReturn corrected SQL only."},
            ]
    return sql, f"Query failed after one repair attempt: {error}", None


# --- charting ---------------------------------------------------------------
def chart_kind(df: pd.DataFrame) -> str | None:
    """Pick a visual from the shape of the result, or None for table-only."""
    if df.empty:
        return None
    numeric = df.select_dtypes("number").columns.tolist()
    if not numeric:
        return None
    if len(df) == 1:
        return "metric"
    if len(df) > 200:
        return None
    dims = [c for c in df.columns if c not in numeric]
    if not dims:
        # every column numeric, e.g. rating vs average salary — first column is the axis
        return "bar" if len(numeric) >= 2 and len(df) <= 30 else None
    x = dims[0]
    if pd.api.types.is_datetime64_any_dtype(df[x]) or re.search(r"date|month|year|week|day", x):
        return "line"
    return "bar" if len(df) <= 30 else None


def render_chart(df: pd.DataFrame) -> None:
    kind = chart_kind(df)
    if kind is None:
        return
    numeric = df.select_dtypes("number").columns.tolist()
    if kind == "metric":
        for col, box in zip(numeric, st.columns(len(numeric))):
            value = df[col].iloc[0]
            box.metric(col.replace("_", " ").title(), f"{value:,.2f}".rstrip("0").rstrip("."))
        return
    dims = [c for c in df.columns if c not in numeric]
    x = dims[0] if dims else df.columns[0]
    values = [c for c in numeric if c != x]
    (st.line_chart if kind == "line" else st.bar_chart)(df.set_index(x)[values])


# --- ui ---------------------------------------------------------------------
def main() -> None:
    st.set_page_config(page_title="Data Q&A", page_icon="📊", layout="wide")
    st.title("📊 Ask your spreadsheets")
    st.caption(f"Model writes the SQL · DuckDB computes the answer · `{MODEL}`")

    if "con" not in st.session_state:
        st.session_state.con = duckdb.connect(":memory:")
        st.session_state.tables = {}
        st.session_state.loaded = set()

    con, tables = st.session_state.con, st.session_state.tables

    uploads = st.file_uploader(
        "Upload CSV or Excel files", type=["csv", "xlsx", "xls"], accept_multiple_files=True
    )
    for upload in uploads or []:
        if upload.name in st.session_state.loaded:
            continue
        try:
            with st.spinner(f"Reading {upload.name}…"):  # a large file takes seconds
                for name, df in load(upload).items():
                    register(con, name, df)
                    tables[name] = df
            st.session_state.loaded.add(upload.name)
        except Exception as e:  # noqa: BLE001 - a bad file shouldn't kill the session
            st.error(f"Could not read {upload.name}: {e}")

    if not tables:
        st.info("Upload at least one CSV or Excel file to get started.")
        return

    with st.sidebar:
        st.subheader("Loaded tables")
        for name, df in tables.items():
            with st.expander(f"{name} · {len(df):,} rows"):
                st.dataframe(df.head(20), width="stretch")
        for hint in join_hints(con, tables):
            st.caption(f"🔗 {hint}")

    question = st.text_input(
        "Your question",
        placeholder="Which region had the highest total order value?",
    )
    if not question:
        return
    if not API_KEY:
        st.error("Set `GROQ_API_KEY` — in `.env` locally, or Streamlit secrets when deployed.")
        return

    with st.spinner("Thinking…"):
        try:
            sql, message, result = ask(question, tables, con)
        except GroqError as e:  # bad key, rate limit, network
            st.error(f"Groq call failed — {e}")
            return

    if message:
        st.warning(message)
    if sql:
        with st.expander("Generated SQL", expanded=message is not None):
            st.code(sql, language="sql")
    if result is None:
        return

    st.success(f"{len(result):,} row{'s' if len(result) != 1 else ''}")
    render_chart(result)
    st.dataframe(result, width="stretch")
    st.download_button("Download CSV", result.to_csv(index=False), "answer.csv", "text/csv")


if __name__ == "__main__":
    main()
