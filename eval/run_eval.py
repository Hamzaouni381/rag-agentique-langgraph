"""Evaluation du pipeline sur eval/questions_demo.txt.

- Avec LANGSMITH_API_KEY : cree/maj un dataset LangSmith et lance une experience comparable
  dans l'UI (une experience par changement de modele, chunking, seuil...).
- Sans cle : meme evaluation en local, resultats dans eval/results/.

    python -m eval.run_eval --limit 20 [--judge]

--judge ajoute un LLM-juge de fidelite (reponse supportee par les passages). Avec un
petit modele local le juge est peu fiable : a utiliser avec LLM_PROVIDER=openai."""
import argparse
import csv
import os
import time
import uuid
from pathlib import Path

from app.graph import get_graph, make_input
from app.prompts import NOT_FOUND

HERE = Path(__file__).parent
DATASET = "rag-v2-questions"


def load_questions(path: Path, limit: int) -> list[str]:
    qs = [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    qs = [q for q in qs if q and not q.startswith("#")]
    return qs[:limit] if limit else qs


def target(inputs: dict) -> dict:
    t0 = time.time()
    out = get_graph().invoke(make_input(inputs["question"]), {"configurable": {"thread_id": f"eval-{uuid.uuid4()}"}})
    return {
        "answer": out.get("answer", ""),
        "route": out.get("route", ""),
        "sources": out.get("sources", []),
        "contexts": [d.page_content for d in out.get("documents", [])],
        "latency_s": round(time.time() - t0, 2),
    }


# ---------- evaluateurs (signature LangSmith : outputs -> score) ----------
def answered(outputs: dict) -> bool:
    return outputs["answer"].strip() != NOT_FOUND


def cited(outputs: dict) -> bool:
    return bool(outputs["sources"]) and "[" in outputs["answer"]


def groundedness(outputs: dict) -> dict:
    from app.llm import get_chat_model

    if not outputs["contexts"] or not answered(outputs):
        return {"key": "groundedness", "score": None}
    prompt = (
        "Les AFFIRMATIONS de la REPONSE sont-elles toutes supportees par les PASSAGES ? "
        "Reponds uniquement 'oui' ou 'non'.\n\nPASSAGES :\n" + "\n---\n".join(outputs["contexts"])
        + f"\n\nREPONSE :\n{outputs['answer']}"
    )
    verdict = get_chat_model().invoke(prompt).content.strip().lower()
    return {"key": "groundedness", "score": int(verdict.startswith("oui"))}


def run_langsmith(questions: list[str], evaluators: list):
    from langsmith import Client

    client = Client()
    if not client.has_dataset(dataset_name=DATASET):
        ds = client.create_dataset(DATASET, description="Questions RAG reprises de la V1")
        client.create_examples(dataset_id=ds.id, inputs=[{"question": q} for q in questions])
    results = client.evaluate(
        target, data=DATASET, evaluators=evaluators, experiment_prefix="rag-v2", max_concurrency=1
    )
    print("Experience LangSmith :", results.experiment_name)


def run_local(questions: list[str], evaluators: list):
    out_dir = HERE / "results"
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"eval_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    rows = []
    for i, q in enumerate(questions, 1):
        o = target({"question": q})
        row = {"question": q, "route": o["route"], "latency_s": o["latency_s"], "answer": o["answer"][:300]}
        for ev in evaluators:
            r = ev(o)
            if isinstance(r, dict):
                row[r["key"]] = r["score"]
            else:
                row[ev.__name__] = int(r)
        rows.append(row)
        print(f"[{i}/{len(questions)}] {row['route']:<9} {row['latency_s']:>6}s  answered={row['answered']}  {q[:70]}")

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)

    def mean(key):
        vals = [r[key] for r in rows if r.get(key) is not None]
        return sum(vals) / len(vals) if vals else float("nan")

    print(f"\n{len(rows)} questions -> {path}")
    for key in [k for k in rows[0] if k not in {"question", "route", "answer"}]:
        print(f"  {key:<14} moyenne = {mean(key):.2f}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--file", default=str(HERE / "questions_demo.txt"))
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--judge", action="store_true")
    args = p.parse_args()

    qs = load_questions(Path(args.file), args.limit)
    evs = [answered, cited] + ([groundedness] if args.judge else [])
    if os.getenv("LANGSMITH_API_KEY"):
        run_langsmith(qs, evs)
    else:
        run_local(qs, evs)
