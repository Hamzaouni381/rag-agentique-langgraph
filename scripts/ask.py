"""Chat en ligne de commande avec memoire : python -m scripts.ask [--thread ID] [--trace]"""
import argparse
import logging
import uuid

from app.graph import get_graph, make_input

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--thread", default=None, help="Reprendre une conversation existante")
    p.add_argument("--trace", action="store_true", help="Afficher les noeuds traverses")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO if args.trace else logging.WARNING)

    thread_id = args.thread or str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    graph = get_graph()
    print(f"Conversation {thread_id} (Ctrl+C pour quitter)")
    while True:
        try:
            q = input("\nVous > ").strip()
        except (KeyboardInterrupt, EOFError):
            break
        if not q:
            continue
        for update in graph.stream(make_input(q), config, stream_mode="updates"):
            if args.trace:
                print("  ->", ", ".join(update))
        out = graph.get_state(config).values
        print(f"\n[{out.get('route')}] {out.get('answer')}")
        if out.get("sql"):
            print(f"  SQL : {out['sql']}")
        for s in out.get("sources", []):
            print(f"  - {s['source']} p.{s.get('page')} (score {s.get('relevance')})")
