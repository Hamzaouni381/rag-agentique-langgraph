"""Ingestion en ligne de commande, dans l'espace de l'utilisateur local :
python -m scripts.ingest [--dir DOSSIER] [--full]"""
import argparse
import json
import logging

from app.auth import LOCAL_USER
from app.ingestion import ingest_directory

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s - %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--dir", default=None, help="Dossier a ingerer (defaut : le dossier de l'utilisateur local)")
    p.add_argument("--full", action="store_true", help="Supprime aussi les chunks des fichiers disparus")
    args = p.parse_args()
    stats = ingest_directory(LOCAL_USER.owner, args.dir, cleanup="full" if args.full else "incremental")
    print(json.dumps(stats, indent=2, ensure_ascii=False, default=str))
